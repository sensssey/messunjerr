"""Подписки (5.4): подписаться, отписаться, убрать подписчика, ответить на запрос, открыть профиль.

**Порядок блокировок.** Он един для всех команд, иначе возможны гонка или взаимная блокировка:

1. строка профиля владельца в `profile.profiles`: подписка читает её `FOR SHARE` (закрытость цели),
   `PATCH /me/profile` держит её `FOR UPDATE` с первой строки команды;
2. замок пары людей (`GraphRepository.lock_pair`), у каждой команды над парой;
3. когда профиль открывается, замки пар берутся друг за другом по возрастанию подписчика.

Гонка закрыта: подписка и открытие профиля конфликтуют на строке профиля и идут друг за другом.
Подписка до открытия оставляет запрос, который открытие потом одобрит (оно читает ждущие запросы уже
под своей строкой); подписка после открытия видит открытый профиль и подписывается сразу. Ждущий
запрос на открытом профиле остаться не может. Команды, которым закрытость цели не нужна (отписка,
удаление подписчика, ответ на запрос, блокировка), строку профиля не трогают и берут сразу замок
пары. Подписка берёт строку `FOR SHARE`, а не `FOR UPDATE`: подписки на один профиль друг друга не
ждут, ждёт только владелец, когда меняет профиль.

Взаимной блокировки нет. Строку профиля берут только `follow_user` и `update_profile` (по одной) и
всегда раньше любого замка пары, значит, держащий замок пары на строку не ждёт. Несколько замков пар
подряд берёт только открытие профиля, и по возрастанию подписчика, а это возрастание по общему
порядку пар (по большему идентификатору, затем по меньшему): ни два, ни три владельца, открывающие
профили разом, цикла ожидания не замкнут. Остальные команды берут ровно один замок пары. Состояние и
события фиксируются одной транзакцией (4.3).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import NotFoundError
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.api_public import find_account
from messunjerr.social.domain import events
from messunjerr.social.domain.errors import follow_request_not_pending, self_action
from messunjerr.social.domain.policies import (
    FollowDecision,
    FollowState,
    ResponseDecision,
    decide_follow,
    decide_follow_request_response,
)
from messunjerr.social.domain.rules import FollowRequestStatus
from messunjerr.social.infra.models import FollowRequestRow
from messunjerr.social.infra.repositories import GraphRepository
from messunjerr.social.queries.cards import load_summaries
from messunjerr.social.queries.follow_models import ApprovedFollower, FollowStatus


@dataclass(frozen=True, slots=True)
class FollowUser:
    actor_id: uuid.UUID
    target_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class UnfollowUser:
    actor_id: uuid.UUID
    target_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RemoveFollower:
    actor_id: uuid.UUID
    follower_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RespondToFollowRequest:
    actor_id: uuid.UUID
    request_id: uuid.UUID


async def follow_user(
    command: FollowUser, *, uow: UnitOfWork, now: datetime | None = None
) -> FollowStatus:
    """`PUT /follows/{user_id}`: подписка на открытый профиль сразу, на закрытый запрос.

    Идемпотентна: повтор по подписке или ждущему запросу отвечает так же и ничего не пишет.
    Строка профиля цели берётся `FOR SHARE` раньше замка пары (порядок блокировок в докстринге
    модуля): закрытость читается тогда, когда её уже никто не меняет до конца транзакции.
    """
    moment = now or utcnow()
    if command.actor_id == command.target_id:
        raise self_action()
    repository = GraphRepository(uow.session)
    target_private = await repository.profile_is_private_for_share(command.target_id)
    await repository.lock_pair(command.actor_id, command.target_id)

    target = await find_account(uow.session, str(command.target_id))
    pending = await repository.pending_follow_request(command.actor_id, command.target_id)
    match decide_follow(
        FollowState(
            is_self=False,
            # аккаунта без профиля для других нет: сбой данных не должен делать его подписываемым
            target_active=target is not None and target.is_active and target_private is not None,
            blocked=await repository.is_blocked_between(command.actor_id, command.target_id),
            following=await repository.is_following(command.actor_id, command.target_id),
            requested=pending is not None,
            target_private=bool(target_private),
        )
    ):
        case FollowDecision.SELF:
            raise self_action()
        case FollowDecision.HIDDEN:
            raise NotFoundError("No such person.")
        case FollowDecision.ALREADY_FOLLOWING:
            return FollowStatus(status="following")
        case FollowDecision.ALREADY_REQUESTED:
            return FollowStatus(status="requested")
        case FollowDecision.FOLLOW:
            await repository.add_follow(command.actor_id, command.target_id, created_at=moment)
            events.record(
                uow.outbox,
                events.FollowCreated(follower_id=command.actor_id, followee_id=command.target_id),
                actor_id=command.actor_id,
            )
            await uow.commit()
            return FollowStatus(status="following")
        case FollowDecision.REQUEST:
            created = await repository.add_follow_request(command.actor_id, command.target_id)
            events.record(
                uow.outbox,
                events.FollowRequested(
                    request_id=created.id,
                    follower_id=command.actor_id,
                    followee_id=command.target_id,
                ),
                actor_id=command.actor_id,
            )
            await uow.commit()
            return FollowStatus(status="requested")


async def unfollow_user(
    command: UnfollowUser, *, uow: UnitOfWork, now: datetime | None = None
) -> None:
    """`DELETE /follows/{user_id}`: отписка либо отмена своего ждущего запроса.

    Ответ всегда `204`, даже если цели нет или она скрыта: ничего лишнего не раскрывается, как у
    `unblock`. `FollowRemoved` пишется, только если подписка была; отмена запроса события не пишет
    (это не исход ответа владельца).
    """
    moment = now or utcnow()
    if command.actor_id == command.target_id:
        return
    repository = GraphRepository(uow.session)
    await repository.lock_pair(command.actor_id, command.target_id)
    removed = await repository.remove_follow(command.actor_id, command.target_id)
    cancelled = await repository.cancel_pending_follow_request(
        command.actor_id, command.target_id, moment
    )
    if removed:
        events.record(
            uow.outbox,
            events.FollowRemoved(follower_id=command.actor_id, followee_id=command.target_id),
            actor_id=command.actor_id,
        )
    if removed or cancelled:
        await uow.commit()


async def remove_follower(command: RemoveFollower, *, uow: UnitOfWork) -> None:
    """`DELETE /me/followers/{user_id}`: владелец убирает подписчика. Всегда `204`.

    Ждущий запрос этого человека (если он есть) остаётся: подписчиком он не стал, и ответить на
    него владелец вправе отдельно. Подписаться заново подписчик может по обычным правилам.
    """
    if command.actor_id == command.follower_id:
        return
    repository = GraphRepository(uow.session)
    await repository.lock_pair(command.actor_id, command.follower_id)
    if not await repository.remove_follow(command.follower_id, command.actor_id):
        return
    events.record(
        uow.outbox,
        events.FollowRemoved(follower_id=command.follower_id, followee_id=command.actor_id),
        actor_id=command.actor_id,
    )
    await uow.commit()


async def _lock_and_load(
    command: RespondToFollowRequest, *, repository: GraphRepository
) -> FollowRequestRow:
    """Запрос под замком его пары; чужой или несуществующий идентификатор это `404`."""
    first = await repository.get_follow_request(command.request_id)
    if first is None:
        raise NotFoundError("No such follow request.")
    await repository.lock_pair(first.follower_id, first.followee_id)
    # Читаем заново под замком: пока мы ждали его, запрос могли одобрить, отменить или закрыть блокировкой.
    current = await repository.get_follow_request(command.request_id)
    if current is None:
        raise NotFoundError("No such follow request.")
    return current


async def _is_active(uow: UnitOfWork, user_id: uuid.UUID) -> bool:
    account = await find_account(uow.session, str(user_id))
    return account is not None and account.is_active


def _check(decision: ResponseDecision) -> None:
    match decision:
        case ResponseDecision.NOT_FOUND:
            raise NotFoundError("No such follow request.")
        case ResponseDecision.NOT_PENDING:
            raise follow_request_not_pending()
        case ResponseDecision.ALLOWED:
            return


async def _approve(
    repository: GraphRepository,
    uow: UnitOfWork,
    request: FollowRequestRow,
    *,
    actor_id: uuid.UUID,
    moment: datetime,
) -> None:
    """Одобряет запрос: исход, подписка и событие. Замок пары уже взят.

    Подписку создаёт сам ответ, поэтому отдельного `FollowCreated` у одобрения нет.
    """
    request.status = FollowRequestStatus.APPROVED
    request.responded_at = moment
    await repository.add_follow(request.follower_id, request.followee_id, created_at=moment)
    events.record(
        uow.outbox,
        events.FollowRequestResponded(
            request_id=request.id,
            follower_id=request.follower_id,
            followee_id=request.followee_id,
            decision=events.FollowRequestDecision.APPROVED,
        ),
        actor_id=actor_id,
    )


async def approve_follow_request(
    command: RespondToFollowRequest, *, uow: UnitOfWork, now: datetime | None = None
) -> ApprovedFollower:
    """`POST /me/follow-requests/{id}/approve`: только владелец; создаёт подписку."""
    moment = now or utcnow()
    repository = GraphRepository(uow.session)
    request = await _lock_and_load(command, repository=repository)
    _check(
        decide_follow_request_response(
            is_owner=request.followee_id == command.actor_id,
            status=FollowRequestStatus(request.status),
            follower_active=await _is_active(uow, request.follower_id),
        )
    )
    await _approve(repository, uow, request, actor_id=command.actor_id, moment=moment)
    summaries = await load_summaries(uow.session, [request.follower_id])
    result = ApprovedFollower(follower=summaries[request.follower_id])
    await uow.commit()
    return result


async def decline_follow_request(
    command: RespondToFollowRequest, *, uow: UnitOfWork, now: datetime | None = None
) -> None:
    """`POST /me/follow-requests/{id}/decline`: только владелец; просивший об отказе не узнаёт."""
    moment = now or utcnow()
    repository = GraphRepository(uow.session)
    request = await _lock_and_load(command, repository=repository)
    _check(
        decide_follow_request_response(
            is_owner=request.followee_id == command.actor_id,
            status=FollowRequestStatus(request.status),
            follower_active=await _is_active(uow, request.follower_id),
        )
    )
    request.status = FollowRequestStatus.DECLINED
    request.responded_at = moment
    events.record(
        uow.outbox,
        events.FollowRequestResponded(
            request_id=request.id,
            follower_id=request.follower_id,
            followee_id=request.followee_id,
            decision=events.FollowRequestDecision.DECLINED,
        ),
        actor_id=command.actor_id,
    )
    await uow.commit()


async def approve_waiting_requests(
    uow: UnitOfWork, *, owner_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Одобряет все ждущие запросы владельца (профиль стал открытым); возвращает, сколько одобрено.

    Идёт по возрастанию подписчика, каждый запрос под замком своей пары. Запрос читается заново под
    замком: пока ждали его, запрос могли отменить, одобрить или закрыть блокировкой. Одобряются и
    запросы от аккаунтов не `active`: подписка скрыта от глаз так же, как и они сами, а оставить
    ждущий запрос на открытом профиле значит навсегда застрять в `requested`. События пишутся от
    имени владельца. Коммита нет: транзакция принадлежит `PATCH /me/profile`.
    """
    moment = now or utcnow()
    repository = GraphRepository(uow.session)
    approved = 0
    for request_id, follower_id in await repository.waiting_follow_requests_of(owner_id):
        await repository.lock_pair(follower_id, owner_id)
        request = await repository.get_follow_request(request_id)
        if request is None or request.status != FollowRequestStatus.PENDING:
            continue
        await _approve(repository, uow, request, actor_id=owner_id, moment=moment)
        approved += 1
    return approved


class OpenedProfileApprovals:
    """Порт `ProfileVisibilityListener` профилей: открытие профиля одобряет ждущие запросы."""

    async def profile_opened(self, uow: UnitOfWork, *, owner_id: uuid.UUID) -> None:
        await approve_waiting_requests(uow, owner_id=owner_id)
