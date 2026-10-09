"""Удаление из друзей `DELETE /friends/{user_id}` (5.4)."""

import uuid
from dataclasses import dataclass

from messunjerr.core.errors import NotFoundError
from messunjerr.core.uow import UnitOfWork
from messunjerr.social.domain import events
from messunjerr.social.domain.rules import ordered_pair
from messunjerr.social.infra.repositories import GraphRepository


@dataclass(frozen=True, slots=True)
class RemoveFriend:
    actor_id: uuid.UUID
    friend_id: uuid.UUID


async def remove_friend(command: RemoveFriend, *, uow: UnitOfWork) -> None:
    """Дружба заканчивается для обоих. Не друзья (и сам себе тоже) это `404`."""
    if command.actor_id == command.friend_id:
        raise NotFoundError("You are not friends.")
    repository = GraphRepository(uow.session)
    await repository.lock_pair(command.actor_id, command.friend_id)
    if not await repository.remove_friendship(command.actor_id, command.friend_id):
        raise NotFoundError("You are not friends.")
    low, high = ordered_pair(command.actor_id, command.friend_id)
    events.record(
        uow.outbox,
        events.FriendshipRemoved(user_low_id=low, user_high_id=high),
        actor_id=command.actor_id,
    )
    await uow.commit()
