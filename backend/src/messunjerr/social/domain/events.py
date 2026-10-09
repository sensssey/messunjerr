"""События социального графа (5.15, топик `mj.social.graph.v1`). Только идентификаторы и перечисления (⚖️).

Ключ партиции это пара `меньший_id:больший_id` (4.8): все события одной пары идут по порядку.
Совершивший действие записывается в заголовок `actor_id` строки outbox: в конверте 6.4 это поле
конверта, а не сообщения, как и `correlation_id`. До S9 события копятся в outbox.
Подписки и запросы на подписку (S8) пишут `FollowCreated`, `FollowRequested`,
`FollowRequestResponded` и `FollowRemoved` в тот же топик и с тем же ключом пары.
"""

import uuid
from dataclasses import asdict, dataclass
from enum import Enum, StrEnum

from messunjerr.core.outbox import Outbox
from messunjerr.social.domain.rules import graph_aggregate

TOPIC_GRAPH = "mj.social.graph.v1"


class Decision(StrEnum):
    """Исход заявки в событии `FriendRequestResponded` (отмена отправителем событием не отмечается)."""

    ACCEPTED = "accepted"
    DECLINED = "declined"


class FollowRequestDecision(StrEnum):
    """Исход запроса на подписку в событии `FollowRequestResponded` (отмену подписчик событием не отмечает)."""

    APPROVED = "approved"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class FriendRequestSent:
    request_id: uuid.UUID
    sender_id: uuid.UUID
    receiver_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FriendRequestResponded:
    """Заявку приняли (в том числе автоматически, когда пришла встречная) или отклонили."""

    request_id: uuid.UUID
    sender_id: uuid.UUID
    receiver_id: uuid.UUID
    decision: Decision


@dataclass(frozen=True, slots=True)
class FriendshipRemoved:
    """Дружба закончилась: человек удалил друга либо один из двоих заблокировал другого."""

    user_low_id: uuid.UUID
    user_high_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class UserBlocked:
    blocker_id: uuid.UUID
    blocked_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class UserUnblocked:
    blocker_id: uuid.UUID
    blocked_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FollowCreated:
    """Подписка появилась сразу: профиль цели был открыт."""

    follower_id: uuid.UUID
    followee_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FollowRequested:
    """Запрос на подписку на закрытый профиль."""

    request_id: uuid.UUID
    follower_id: uuid.UUID
    followee_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FollowRequestResponded:
    """Запрос одобрен (подписку создаёт сам ответ: отдельного `FollowCreated` нет) или отклонён.

    Одобрение всех ждущих запросов при открытии профиля пишет такие же события от имени владельца.
    """

    request_id: uuid.UUID
    follower_id: uuid.UUID
    followee_id: uuid.UUID
    decision: FollowRequestDecision


@dataclass(frozen=True, slots=True)
class FollowRemoved:
    """Подписка закончилась: отписка, удаление подписчика владельцем или блокировка любой из сторон."""

    follower_id: uuid.UUID
    followee_id: uuid.UUID


type GraphEvent = (
    FriendRequestSent
    | FriendRequestResponded
    | FriendshipRemoved
    | UserBlocked
    | UserUnblocked
    | FollowCreated
    | FollowRequested
    | FollowRequestResponded
    | FollowRemoved
)


def _pair_of(event: GraphEvent) -> tuple[uuid.UUID, uuid.UUID]:
    match event:
        case FriendRequestSent(sender_id=first, receiver_id=second):
            return first, second
        case FriendRequestResponded(sender_id=first, receiver_id=second):
            return first, second
        case FriendshipRemoved(user_low_id=first, user_high_id=second):
            return first, second
        case UserBlocked(blocker_id=first, blocked_id=second):
            return first, second
        case UserUnblocked(blocker_id=first, blocked_id=second):
            return first, second
        case FollowCreated(follower_id=first, followee_id=second):
            return first, second
        case FollowRequested(follower_id=first, followee_id=second):
            return first, second
        case FollowRequestResponded(follower_id=first, followee_id=second):
            return first, second
        case FollowRemoved(follower_id=first, followee_id=second):
            return first, second


def record(outbox: Outbox, event: GraphEvent, *, actor_id: uuid.UUID) -> None:
    """Кладёт событие в outbox текущей транзакции."""
    first, second = _pair_of(event)
    payload: dict[str, str] = {
        name: value.value if isinstance(value, Enum) else str(value)
        for name, value in asdict(event).items()
    }
    outbox.add(
        topic=TOPIC_GRAPH,
        key=graph_aggregate(first, second),
        event_type=type(event).__name__,
        payload=payload,
        headers={"actor_id": str(actor_id)},
    )
