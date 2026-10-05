"""Порт отправки почты и ошибки: контекстам и задачам важно лишь, стоит ли повторять отправку."""

from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol


class MailError(Exception):
    """Письмо не отправлено."""


class TransientMailError(MailError):
    """Временный сбой (сеть, ответ 4xx): отправку стоит повторить позже."""


class PermanentMailError(MailError):
    """Повтор бессмыслен: адрес отвергнут (5xx), неверные учётные данные, письмо некорректно."""


class Mailer(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Отправляет письмо; при неудаче бросает `TransientMailError` или `PermanentMailError`."""
        ...


@dataclass(slots=True)
class InMemoryMailer:
    """Почтовый ящик в памяти для тестов."""

    sent: list[EmailMessage] = field(default_factory=list[EmailMessage])
    fail_with: MailError | None = None

    async def send(self, message: EmailMessage) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
