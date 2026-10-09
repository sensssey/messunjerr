"""Блокировки: `PUT` и `DELETE /blocks/{user_id}` (5.4, 4.6).

«A блокирует B»: в одной транзакции под замком пары появляется блокировка, исчезает дружба и
подписки в обе стороны, отменяются активная заявка в друзья и ждущие запросы на подписку в обе
стороны. Оба действия идемпотентны: повтор не меняет состояние и не пишет события. Взаимной блокировки
не бывает: тот, кто заблокировал вас, для вас скрыт, и блокировка в ответ отвечает `404`, как любой
скрытый человек (4.6).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from messunjerr.core.clock import utcnow
from messunjerr.core.errors import NotFoundError
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.api_public import find_account
from messunjerr.social.domain import events
from messunjerr.social.domain.errors import self_action
from messunjerr.social.domain.policies import BlockDecision, decide_block
from messunjerr.social.domain.rules import ordered_pair
from messunjerr.social.infra.repositories import GraphRepository


@dataclass(frozen=True, slots=True)
class BlockUser:
    actor_id: uuid.UUID
    target_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class UnblockUser:
    actor_id: uuid.UUID
    target_id: uuid.UUID


async def block_user(command: BlockUser, *, uow: UnitOfWork, now: datetime | None = None) -> None:
    moment = now or utcnow()
    is_self = command.actor_id == command.target_id
    repository = GraphRepository(uow.session)
    # Замок первым: всё, что решает команда (кто цель, есть ли блокировка в ту и другую сторону),
    # читается уже тогда, когда пару никто не меняет до конца транзакции.
    await repository.lock_pair(command.actor_id, command.target_id)
    if not is_self and await repository.has_block(command.actor_id, command.target_id):
        return  # уже заблокирован: повтор ничего не меняет и событий не пишет, кем бы цель ни стала
    target = None if is_self else await find_account(uow.session, str(command.target_id))
    match decide_block(
        is_self=is_self,
        target_active=target is not None and target.is_active,
        blocked_by_target=not is_self
        and await repository.has_block(command.target_id, command.actor_id),
    ):
        case BlockDecision.SELF:
            raise self_action()
        case BlockDecision.HIDDEN:
            raise NotFoundError("No such person.")
        case BlockDecision.ALLOWED:
            pass

    await repository.add_block(command.actor_id, command.target_id)
    had_friendship = await repository.remove_friendship(command.actor_id, command.target_id)
    await repository.cancel_pending_between(command.actor_id, command.target_id, moment)
    removed_follows = await repository.remove_follows_between(command.actor_id, command.target_id)
    await repository.cancel_follow_requests_between(command.actor_id, command.target_id, moment)
    events.record(
        uow.outbox,
        events.UserBlocked(blocker_id=command.actor_id, blocked_id=command.target_id),
        actor_id=command.actor_id,
    )
    if had_friendship:
        low, high = ordered_pair(command.actor_id, command.target_id)
        events.record(
            uow.outbox,
            events.FriendshipRemoved(user_low_id=low, user_high_id=high),
            actor_id=command.actor_id,
        )
    # По событию на каждую снятую подписку; запросы на подписку закрываются без событий (отмена).
    for follower_id, followee_id in removed_follows:
        events.record(
            uow.outbox,
            events.FollowRemoved(follower_id=follower_id, followee_id=followee_id),
            actor_id=command.actor_id,
        )
    await uow.commit()


async def unblock_user(command: UnblockUser, *, uow: UnitOfWork) -> None:
    """Снимает блокировку; дружба и заявки не восстанавливаются. Нет блокировки или нет человека: тоже `204`."""
    if command.actor_id == command.target_id:
        return
    repository = GraphRepository(uow.session)
    await repository.lock_pair(command.actor_id, command.target_id)
    if not await repository.remove_block(command.actor_id, command.target_id):
        return
    events.record(
        uow.outbox,
        events.UserUnblocked(blocker_id=command.actor_id, blocked_id=command.target_id),
        actor_id=command.actor_id,
    )
    await uow.commit()
