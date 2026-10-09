"""Порты профилей к контекстам выше: медиа (S6), социальный граф (S7–S8), контент (S11).

`media`, `social` и `content` стоят выше `profiles` в графе 4.2 и импортировать их отсюда нельзя.
Пока их нет, корень приложения подставляет заглушки из `profiles.infra.stubs`; каждый следующий
спринт заменяет свою заглушку настоящей реализацией при сборке приложения (`messunjerr.main`),
а правила профиля при этом не меняются.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import MeCounters
from messunjerr.core.uow import UnitOfWork
from messunjerr.profiles.domain.policies import Relation

Friendship = Literal["none", "friends", "request_sent", "request_received"]
Following = Literal["none", "following", "requested"]


class AvatarCheck(StrEnum):
    """Можно ли назначить ресурс аватаром (5.3): коды элемента ошибки `/body/avatar_asset_id`."""

    OK = "ok"
    NOT_FOUND = "asset_not_found"
    """Нет такого ресурса или он чужой (чужие идентификаторы не проверить перебором)."""
    NOT_READY = "asset_not_ready"
    WRONG_PURPOSE = "asset_wrong_purpose"


class AvatarAssets(Protocol):
    async def check(
        self, session: AsyncSession, *, owner_id: uuid.UUID, asset_id: uuid.UUID
    ) -> AvatarCheck:
        """Можно ли назначить ресурс аватаром. Строка ресурса блокируется до конца транзакции:
        параллельное удаление ресурса не успеет вклиниться между проверкой и назначением."""
        ...

    async def release(self, uow: UnitOfWork, *, owner_id: uuid.UUID, asset_id: uuid.UUID) -> None:
        """Прежний аватар заменён или убран: ресурс больше не нужен и удаляется вместе с объектами
        (в той же транзакции, объекты убирает фоновая задача после коммита)."""
        ...


@dataclass(frozen=True, slots=True)
class RelationshipView:
    """`Relationship` из 5.1: как зритель связан с владельцем профиля."""

    is_self: bool
    friendship: Friendship = "none"
    friend_request_id: uuid.UUID | None = None
    following: Following = "none"
    follows_you: bool = False
    blocked: bool = False
    """Зритель заблокировал владельца."""
    blocked_by_owner: bool = False
    """Владелец заблокировал зрителя; наружу не отдаётся: ресурс для зрителя просто `404` (4.6)."""

    @property
    def relation(self) -> Relation:
        """Сведение к состоянию из матрицы 4.6: у блокировки (любой стороны) приоритет, затем
        дружба, затем подписка."""
        if self.is_self:
            return Relation.SELF
        if self.blocked or self.blocked_by_owner:
            return Relation.BLOCKED
        if self.friendship == "friends":
            return Relation.FRIEND
        if self.following == "following":
            return Relation.FOLLOWER
        return Relation.STRANGER


class Relationships(Protocol):
    async def between(
        self, session: AsyncSession, *, viewer_id: uuid.UUID, owner_id: uuid.UUID
    ) -> RelationshipView: ...


@dataclass(frozen=True, slots=True)
class ProfileCounts:
    posts: int = 0
    friends: int = 0
    followers: int = 0
    following: int = 0


class ProfileCounters(Protocol):
    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> ProfileCounts: ...


class MeCountersSource(Protocol):
    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> MeCounters:
        """Счётчики шапки клиента для `GET /me` (5.3): входящие заявки в друзья и запросы на подписку
        даёт social, позже уведомления (S10) и беседы (S14) добавят свои части."""
        ...


class ProfileVisibilityListener(Protocol):
    """Реакция контекстов выше на смену закрытости профиля (5.3, S8)."""

    async def profile_opened(self, uow: UnitOfWork, *, owner_id: uuid.UUID) -> None:
        """Профиль стал открытым (`is_private`: `true` → `false`).

        Вызывается из `PATCH /me/profile` ВНУТРИ его транзакции, до фиксации и после того, как
        команда взяла строку профиля владельца `FOR UPDATE`: social одобряет все ждущие запросы на
        подписку этого владельца, и они фиксируются вместе со сменой закрытости. Порядок блокировок
        «строка профиля, затем замок пары» держат все команды подписки (`social.commands.follows`).

        Правило для новых писателей: сегодня `is_private` из `true` в `false` переводит только
        `update_profile`. Любой другой код с таким переходом (админка, модерация) обязан вызвать этот
        порт под строкой профиля `FOR UPDATE`, иначе на открытом профиле навсегда останется ждущий
        запрос (спецификация 4.6, «Правило для новых писателей»).
        """
        ...
