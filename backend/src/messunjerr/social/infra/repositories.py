"""Репозиторий социального графа: запись и точечные проверки команд.

Репозиторий `commit()` не вызывает (4.3). Все команды над одной парой людей сначала берут замок пары
(`lock_pair`): заявки друг другу в один миг, ответ на заявку при блокировке, удаление друга при
принятии идут друг за другом, а не вперемешку. Замок снимается вместе с транзакцией.

Единственное, что берётся раньше замка пары, это строка профиля цели подписки `FOR SHARE`
(`profile_is_private_for_share`): порядок «строка профиля владельца, затем замок пары» един для
подписки и для открытия профиля (`social.commands.follows`).
"""

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.social.domain.rules import FollowRequestStatus, FriendRequestStatus, ordered_pair
from messunjerr.social.infra.directory import profiles
from messunjerr.social.infra.models import (
    BlockRow,
    FollowRequestRow,
    FollowRow,
    FriendRequestRow,
    FriendshipRow,
    is_follow_pending,
    is_pending,
)


def pair_lock_key(first: uuid.UUID, second: uuid.UUID) -> int:
    """Ключ рекомендательного замка пары: 64 бита хэша упорядоченной пары (знак нужен PostgreSQL)."""
    low, high = ordered_pair(first, second)
    digest = hashlib.blake2b(low.bytes + high.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class GraphRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_pair(self, first: uuid.UUID, second: uuid.UUID) -> None:
        """Замок на пару до конца транзакции; порядок людей не важен."""
        await self._session.execute(
            select(func.pg_advisory_xact_lock(pair_lock_key(first, second)))
        )

    # --- дружба
    async def are_friends(self, first: uuid.UUID, second: uuid.UUID) -> bool:
        low, high = ordered_pair(first, second)
        found = await self._session.execute(
            select(FriendshipRow.user_low_id).where(
                FriendshipRow.user_low_id == low, FriendshipRow.user_high_id == high
            )
        )
        return found.first() is not None

    async def add_friendship(
        self, first: uuid.UUID, second: uuid.UUID, *, created_at: datetime
    ) -> None:
        """Дружба появляется один раз; повторная вставка ничего не меняет."""
        low, high = ordered_pair(first, second)
        await self._session.execute(
            insert(FriendshipRow)
            .values(user_low_id=low, user_high_id=high, created_at=created_at)
            .on_conflict_do_nothing()
        )

    async def remove_friendship(self, first: uuid.UUID, second: uuid.UUID) -> bool:
        """`True`, если дружба была."""
        low, high = ordered_pair(first, second)
        removed = await self._session.execute(
            delete(FriendshipRow)
            .where(FriendshipRow.user_low_id == low, FriendshipRow.user_high_id == high)
            .returning(FriendshipRow.user_low_id)
        )
        return removed.first() is not None

    # --- заявки
    async def get_request(self, request_id: uuid.UUID) -> FriendRequestRow | None:
        return (
            await self._session.execute(
                select(FriendRequestRow)
                .where(FriendRequestRow.id == request_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def pending_between(self, first: uuid.UUID, second: uuid.UUID) -> FriendRequestRow | None:
        """Активная заявка между двумя людьми, в любом направлении (не больше одной: уникальный индекс)."""
        low, high = ordered_pair(first, second)
        return (
            await self._session.execute(
                select(FriendRequestRow)
                .where(
                    is_pending(),
                    func.least(FriendRequestRow.sender_id, FriendRequestRow.receiver_id) == low,
                    func.greatest(FriendRequestRow.sender_id, FriendRequestRow.receiver_id) == high,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def add_request(self, sender_id: uuid.UUID, receiver_id: uuid.UUID) -> FriendRequestRow:
        row = FriendRequestRow(
            sender_id=sender_id, receiver_id=receiver_id, status=FriendRequestStatus.PENDING
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def cancel_pending_between(
        self, first: uuid.UUID, second: uuid.UUID, moment: datetime
    ) -> int:
        """Отменяет активную заявку пары (блокировка); возвращает, сколько заявок закрыто."""
        low, high = ordered_pair(first, second)
        result = await self._session.execute(
            update(FriendRequestRow)
            .where(
                is_pending(),
                func.least(FriendRequestRow.sender_id, FriendRequestRow.receiver_id) == low,
                func.greatest(FriendRequestRow.sender_id, FriendRequestRow.receiver_id) == high,
            )
            .values(status=FriendRequestStatus.CANCELLED, responded_at=moment)
            .returning(FriendRequestRow.id)
        )
        return len(result.all())

    # --- блокировки
    async def is_blocked_between(self, first: uuid.UUID, second: uuid.UUID) -> bool:
        """Блокировка в любую сторону."""
        found = await self._session.execute(
            select(BlockRow.blocker_id).where(
                or_(
                    and_(BlockRow.blocker_id == first, BlockRow.blocked_id == second),
                    and_(BlockRow.blocker_id == second, BlockRow.blocked_id == first),
                )
            )
        )
        return found.first() is not None

    async def has_block(self, blocker_id: uuid.UUID, blocked_id: uuid.UUID) -> bool:
        """Блокировка в одну сторону: `blocker_id` заблокировал `blocked_id`."""
        found = await self._session.execute(
            select(BlockRow.blocker_id).where(
                BlockRow.blocker_id == blocker_id, BlockRow.blocked_id == blocked_id
            )
        )
        return found.first() is not None

    async def add_block(self, blocker_id: uuid.UUID, blocked_id: uuid.UUID) -> None:
        """Добавляет блокировку. Команда уже проверила под замком пары, что ни этой, ни встречной
        блокировки нет, поэтому конфликт ключа здесь молча не глотается: уникальный индекс пары
        (`ux_blocks_pair`) уронит запрос, если проверку когда-нибудь забудут."""
        await self._session.execute(
            insert(BlockRow).values(blocker_id=blocker_id, blocked_id=blocked_id)
        )

    async def remove_block(self, blocker_id: uuid.UUID, blocked_id: uuid.UUID) -> bool:
        """`True`, если блокировка была."""
        removed = await self._session.execute(
            delete(BlockRow)
            .where(BlockRow.blocker_id == blocker_id, BlockRow.blocked_id == blocked_id)
            .returning(BlockRow.blocker_id)
        )
        return removed.first() is not None

    # --- закрытость профиля цели подписки
    async def profile_is_private_for_share(self, user_id: uuid.UUID) -> bool | None:
        """Закрыт ли профиль человека. Строка профиля блокируется `FOR SHARE` до конца транзакции.

        Блокировка нужна, чтобы закрытость не изменилась между чтением и подпиской: открытие профиля
        (`PATCH /me/profile`) держит ту же строку `FOR UPDATE` и одобряет ждущие запросы, поэтому
        подписка идёт либо целиком до него (и будет одобрена им), либо целиком после (и увидит
        открытый профиль). `FOR SHARE`, а не `FOR UPDATE`: подписки на один профиль друг друга не
        ждут. Читать строку нужно ДО замка пары: порядок «строка профиля, затем замок пары» един
        для всех, иначе возможна взаимная блокировка. `None`, если профиля нет.
        """
        found = await self._session.execute(
            select(profiles.c.is_private)
            .where(profiles.c.user_id == user_id)
            .with_for_update(read=True)
        )
        row = found.first()
        return None if row is None else bool(row.is_private)

    # --- подписки
    async def is_following(self, follower_id: uuid.UUID, followee_id: uuid.UUID) -> bool:
        found = await self._session.execute(
            select(FollowRow.follower_id).where(
                FollowRow.follower_id == follower_id, FollowRow.followee_id == followee_id
            )
        )
        return found.first() is not None

    async def add_follow(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID, *, created_at: datetime
    ) -> None:
        """Добавляет подписку. Команда уже проверила под замком пары, что её нет, поэтому конфликт ключа
        молча не глотается: первичный ключ уронит запрос, если проверку когда-нибудь забудут."""
        await self._session.execute(
            insert(FollowRow).values(
                follower_id=follower_id, followee_id=followee_id, created_at=created_at
            )
        )

    async def remove_follow(self, follower_id: uuid.UUID, followee_id: uuid.UUID) -> bool:
        """`True`, если подписка была."""
        removed = await self._session.execute(
            delete(FollowRow)
            .where(FollowRow.follower_id == follower_id, FollowRow.followee_id == followee_id)
            .returning(FollowRow.follower_id)
        )
        return removed.first() is not None

    async def remove_follows_between(
        self, first: uuid.UUID, second: uuid.UUID
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Удаляет подписки пары в обе стороны (блокировка); возвращает удалённые пары
        `(подписчик, на кого подписан)` по возрастанию подписчика, чтобы события шли в одном порядке."""
        removed = await self._session.execute(
            delete(FollowRow)
            .where(
                or_(
                    and_(FollowRow.follower_id == first, FollowRow.followee_id == second),
                    and_(FollowRow.follower_id == second, FollowRow.followee_id == first),
                )
            )
            .returning(FollowRow.follower_id, FollowRow.followee_id)
        )
        return sorted((row.follower_id, row.followee_id) for row in removed)

    # --- запросы на подписку
    async def get_follow_request(self, request_id: uuid.UUID) -> FollowRequestRow | None:
        return (
            await self._session.execute(
                select(FollowRequestRow)
                .where(FollowRequestRow.id == request_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def pending_follow_request(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID
    ) -> FollowRequestRow | None:
        """Ждущий запрос `follower_id` к `followee_id` (не больше одного: уникальный индекс)."""
        return (
            await self._session.execute(
                select(FollowRequestRow)
                .where(
                    is_follow_pending(),
                    FollowRequestRow.follower_id == follower_id,
                    FollowRequestRow.followee_id == followee_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def add_follow_request(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID
    ) -> FollowRequestRow:
        """Новый запрос. Как и подписка, без заглушки конфликта: частичный уникальный индекс
        `ux_follow_requests_pending` уронит запрос, если команда забудет проверку под замком пары."""
        row = FollowRequestRow(
            follower_id=follower_id, followee_id=followee_id, status=FollowRequestStatus.PENDING
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def cancel_pending_follow_request(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID, moment: datetime
    ) -> bool:
        """Отменяет ждущий запрос `follower_id` к `followee_id` (отписка); `True`, если он был."""
        result = await self._session.execute(
            update(FollowRequestRow)
            .where(
                is_follow_pending(),
                FollowRequestRow.follower_id == follower_id,
                FollowRequestRow.followee_id == followee_id,
            )
            .values(status=FollowRequestStatus.CANCELLED, responded_at=moment)
            .returning(FollowRequestRow.id)
        )
        return result.first() is not None

    async def cancel_follow_requests_between(
        self, first: uuid.UUID, second: uuid.UUID, moment: datetime
    ) -> int:
        """Отменяет ждущие запросы пары в обе стороны (блокировка); возвращает, сколько закрыто."""
        result = await self._session.execute(
            update(FollowRequestRow)
            .where(
                is_follow_pending(),
                or_(
                    and_(
                        FollowRequestRow.follower_id == first,
                        FollowRequestRow.followee_id == second,
                    ),
                    and_(
                        FollowRequestRow.follower_id == second,
                        FollowRequestRow.followee_id == first,
                    ),
                ),
            )
            .values(status=FollowRequestStatus.CANCELLED, responded_at=moment)
            .returning(FollowRequestRow.id)
        )
        return len(result.all())

    async def waiting_follow_requests_of(
        self, followee_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Ждущие запросы к владельцу как пары `(запрос, подписчик)` по возрастанию подписчика.

        Порядок один и тот же у всех, кто берёт несколько замков пар подряд (открытие профиля): так
        замки идут по одному возрастающему ключу и взаимной блокировки быть не может.
        """
        rows = await self._session.execute(
            select(FollowRequestRow.id, FollowRequestRow.follower_id)
            .where(is_follow_pending(), FollowRequestRow.followee_id == followee_id)
            .order_by(FollowRequestRow.follower_id)
        )
        return [(row.id, row.follower_id) for row in rows]
