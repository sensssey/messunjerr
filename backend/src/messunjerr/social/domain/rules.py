"""Правила социального графа (4.5, 4.6): статусы заявок и пара пользователей без порядка.

Чистые функции и перечисления без ввода-вывода: их проверяют unit-тесты, а команды и запросы
только применяют результат.
"""

import uuid
from enum import StrEnum


class FriendRequestStatus(StrEnum):
    """Состояние заявки в друзья (4.5): активна только `pending`, остальное её исходы."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


FRIEND_REQUEST_STATUSES: tuple[str, ...] = tuple(item.value for item in FriendRequestStatus)


class FollowRequestStatus(StrEnum):
    """Состояние запроса на подписку на закрытый профиль (4.5): активен только `pending`."""

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"


FOLLOW_REQUEST_STATUSES: tuple[str, ...] = tuple(item.value for item in FollowRequestStatus)


class Direction(StrEnum):
    """Чья заявка относительно зрителя (5.4): `incoming` пришла ему, `outgoing` отправил он."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"


def ordered_pair(first: uuid.UUID, second: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Пара в порядке хранения: меньший идентификатор первым (`user_low_id < user_high_id`).

    Идентификаторы сравниваются как 128-битные числа, так же сравнивает `uuid` PostgreSQL, поэтому
    ограничение `CHECK (user_low_id < user_high_id)` и этот порядок всегда совпадают.
    """
    return (first, second) if first.int < second.int else (second, first)


def graph_aggregate(first: uuid.UUID, second: uuid.UUID) -> str:
    """Ключ партиции событий графа `меньший_id:больший_id` (4.8): события одной пары идут по порядку."""
    low, high = ordered_pair(first, second)
    return f"{low}:{high}"
