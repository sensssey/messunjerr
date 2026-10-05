"""События контекста identity (5.15). Только идентификаторы: ни почты, ни имён (⚖️)."""

import uuid
from dataclasses import dataclass

from messunjerr.core.outbox import Outbox

TOPIC_USER = "mj.identity.user.v1"


@dataclass(frozen=True, slots=True)
class UserRegistered:
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class EmailVerified:
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PasswordChanged:
    """Пароль сменён или сброшен: потребителей в v1 нет (задел под уведомления и аналитику)."""

    user_id: uuid.UUID


type UserEvent = UserRegistered | EmailVerified | PasswordChanged


def record(outbox: Outbox, event: UserEvent) -> None:
    """Кладёт событие в outbox текущей транзакции; ключ партиции — пользователь."""
    outbox.add(
        topic=TOPIC_USER,
        key=str(event.user_id),
        event_type=type(event).__name__,
        payload={"user_id": str(event.user_id)},
    )
