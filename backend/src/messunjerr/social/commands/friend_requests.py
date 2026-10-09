"""Заявки в друзья (5.4): отправить, принять, отклонить, отменить.

Каждая команда сначала берёт замок пары людей (`GraphRepository.lock_pair`) и только потом читает
состояние: две заявки друг другу в один миг, принятие при отмене, ответ при блокировке идут друг за
другом. Состояние и событие outbox фиксируются одной транзакцией (4.3). Встречная заявка принимается
сразу: встречающиеся заявки дают ровно одну дружбу.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import NotFoundError
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.api_public import find_account
from messunjerr.social.domain import events
from messunjerr.social.domain.errors import (
    already_friends,
    friend_request_exists,
    friend_request_not_pending,
    self_action,
)
from messunjerr.social.domain.policies import (
    PairState,
    ResponseDecision,
    SendDecision,
    decide_friend_request,
    decide_response,
)
from messunjerr.social.domain.rules import Direction, FriendRequestStatus
from messunjerr.social.infra.models import FriendRequestRow
from messunjerr.social.infra.repositories import GraphRepository
from messunjerr.social.queries.cards import load_summaries
from messunjerr.social.queries.friend_requests import friend_request_card
from messunjerr.social.queries.models import AcceptedFriend, FriendRequest


@dataclass(frozen=True, slots=True)
class SendFriendRequest:
    sender_id: uuid.UUID
    receiver_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FriendRequestSentResult:
    request: FriendRequest
    created: bool
    """`True`: создана новая заявка (`201`); `False`: приняла встречную (`200`, статус `accepted`)."""


@dataclass(frozen=True, slots=True)
class RespondToRequest:
    actor_id: uuid.UUID
    request_id: uuid.UUID


def _direction(request: FriendRequestRow | None, actor_id: uuid.UUID) -> Direction | None:
    if request is None:
        return None
    return Direction.OUTGOING if request.sender_id == actor_id else Direction.INCOMING


async def _accept(
    repository: GraphRepository,
    uow: UnitOfWork,
    request: FriendRequestRow,
    *,
    actor_id: uuid.UUID,
    moment: datetime,
) -> None:
    """Принимает заявку: исход, дружба и событие. Замок пары уже взят."""
    request.status = FriendRequestStatus.ACCEPTED
    request.responded_at = moment
    await repository.add_friendship(request.sender_id, request.receiver_id, created_at=moment)
    events.record(
        uow.outbox,
        events.FriendRequestResponded(
            request_id=request.id,
            sender_id=request.sender_id,
            receiver_id=request.receiver_id,
            decision=events.Decision.ACCEPTED,
        ),
        actor_id=actor_id,
    )


async def send_friend_request(
    command: SendFriendRequest, *, uow: UnitOfWork, now: datetime | None = None
) -> FriendRequestSentResult:
    """`POST /friend-requests`: новая заявка либо автопринятие встречной."""
    moment = now or utcnow()
    if command.sender_id == command.receiver_id:
        raise self_action()
    repository = GraphRepository(uow.session)
    await repository.lock_pair(command.sender_id, command.receiver_id)

    target = await find_account(uow.session, str(command.receiver_id))
    pending = await repository.pending_between(command.sender_id, command.receiver_id)
    decision = decide_friend_request(
        PairState(
            is_self=False,
            target_active=target is not None and target.is_active,
            blocked=await repository.is_blocked_between(command.sender_id, command.receiver_id),
            friends=await repository.are_friends(command.sender_id, command.receiver_id),
            pending=_direction(pending, command.sender_id),
        )
    )
    match decision:
        case SendDecision.SELF:
            raise self_action()
        case SendDecision.HIDDEN:
            raise NotFoundError("No such person.")
        case SendDecision.ALREADY_FRIENDS:
            raise already_friends()
        case SendDecision.ALREADY_SENT:
            raise friend_request_exists()
        case SendDecision.ACCEPT_INCOMING:
            if pending is None:  # недостижимо под замком пары; безопасный ответ вместо падения
                raise NotFoundError("No such person.")
            await _accept(repository, uow, pending, actor_id=command.sender_id, moment=moment)
            card = await friend_request_card(uow.session, pending, viewer_id=command.sender_id)
            await uow.commit()
            return FriendRequestSentResult(request=card, created=False)
        case SendDecision.CREATE:
            created = await repository.add_request(command.sender_id, command.receiver_id)
            events.record(
                uow.outbox,
                events.FriendRequestSent(
                    request_id=created.id,
                    sender_id=created.sender_id,
                    receiver_id=created.receiver_id,
                ),
                actor_id=command.sender_id,
            )
            card = await friend_request_card(uow.session, created, viewer_id=command.sender_id)
            await uow.commit()
            return FriendRequestSentResult(request=card, created=True)


async def _lock_and_load(
    command: RespondToRequest, *, uow: UnitOfWork, repository: GraphRepository
) -> FriendRequestRow:
    """Заявка под замком её пары; чужой или несуществующий идентификатор это `404`."""
    first = await repository.get_request(command.request_id)
    if first is None:
        raise NotFoundError("No such friend request.")
    await repository.lock_pair(first.sender_id, first.receiver_id)
    # Читаем заново под замком: пока мы ждали его, заявку могли принять, отменить или закрыть блокировкой.
    current = await repository.get_request(command.request_id)
    if current is None:
        raise NotFoundError("No such friend request.")
    return current


async def _is_active(uow: UnitOfWork, user_id: uuid.UUID) -> bool:
    account = await find_account(uow.session, str(user_id))
    return account is not None and account.is_active


def _check(decision: ResponseDecision) -> None:
    match decision:
        case ResponseDecision.NOT_FOUND:
            raise NotFoundError("No such friend request.")
        case ResponseDecision.NOT_PENDING:
            raise friend_request_not_pending()
        case ResponseDecision.ALLOWED:
            return


async def accept_friend_request(
    command: RespondToRequest, *, uow: UnitOfWork, now: datetime | None = None
) -> AcceptedFriend:
    """`POST /friend-requests/{id}/accept`: только получатель; создаёт дружбу."""
    moment = now or utcnow()
    repository = GraphRepository(uow.session)
    request = await _lock_and_load(command, uow=uow, repository=repository)
    _check(
        decide_response(
            is_addressee=request.receiver_id == command.actor_id,
            status=FriendRequestStatus(request.status),
            other_active=await _is_active(uow, request.sender_id),
        )
    )
    await _accept(repository, uow, request, actor_id=command.actor_id, moment=moment)
    summaries = await load_summaries(uow.session, [request.sender_id])
    result = AcceptedFriend(friend=summaries[request.sender_id], since=moment)
    await uow.commit()
    return result


async def decline_friend_request(
    command: RespondToRequest, *, uow: UnitOfWork, now: datetime | None = None
) -> None:
    """`POST /friend-requests/{id}/decline`: только получатель; отправитель об отказе не узнаёт."""
    moment = now or utcnow()
    repository = GraphRepository(uow.session)
    request = await _lock_and_load(command, uow=uow, repository=repository)
    _check(
        decide_response(
            is_addressee=request.receiver_id == command.actor_id,
            status=FriendRequestStatus(request.status),
            other_active=await _is_active(uow, request.sender_id),
        )
    )
    request.status = FriendRequestStatus.DECLINED
    request.responded_at = moment
    events.record(
        uow.outbox,
        events.FriendRequestResponded(
            request_id=request.id,
            sender_id=request.sender_id,
            receiver_id=request.receiver_id,
            decision=events.Decision.DECLINED,
        ),
        actor_id=command.actor_id,
    )
    await uow.commit()


async def cancel_friend_request(
    command: RespondToRequest, *, uow: UnitOfWork, now: datetime | None = None
) -> None:
    """`DELETE /friend-requests/{id}`: только отправитель. События нет: отмена не исход ответа (5.15)."""
    moment = now or utcnow()
    repository = GraphRepository(uow.session)
    request = await _lock_and_load(command, uow=uow, repository=repository)
    _check(
        decide_response(
            is_addressee=request.sender_id == command.actor_id,
            status=FriendRequestStatus(request.status),
            other_active=await _is_active(uow, request.receiver_id),
        )
    )
    request.status = FriendRequestStatus.CANCELLED
    request.responded_at = moment
    await uow.commit()
