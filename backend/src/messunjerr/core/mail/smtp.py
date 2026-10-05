"""Отправка по SMTP через aiosmtplib. Адрес сервера задаёт `SMTP_URL`:

- `smtp://[пользователь:пароль@]хост[:порт]`: без шифрования с самого начала (порт 25), STARTTLS
  включается, если сервер его предлагает; так работает Mailpit в разработке;
- `smtp+starttls://…`: STARTTLS обязателен (порт 587);
- `smtps://…`: TLS с первого байта (порт 465).

⚖️ Письма в проде идут через провайдера с серверами в РФ (спецификация 3.1).
"""

from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit

import aiosmtplib

from messunjerr.core.mail.base import PermanentMailError, TransientMailError

_DEFAULT_PORTS = {"smtp": 25, "smtp+starttls": 587, "smtps": 465}


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    hostname: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    start_tls: bool | None

    def __repr__(self) -> str:  # пароль в логи и трассировки не попадает
        return f"SmtpConfig({self.hostname}:{self.port}, tls={self.use_tls}, starttls={self.start_tls})"


def parse_smtp_url(url: str) -> SmtpConfig:
    parts = urlsplit(url)
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError("SMTP_URL: ожидается smtp://хост[:порт], smtp+starttls://… или smtps://…")
    return SmtpConfig(
        hostname=parts.hostname,
        port=parts.port or _DEFAULT_PORTS[parts.scheme],
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
        use_tls=parts.scheme == "smtps",
        start_tls=True if parts.scheme == "smtp+starttls" else None,
    )


def _is_permanent(error: aiosmtplib.SMTPException) -> bool:
    if isinstance(error, aiosmtplib.SMTPRecipientsRefused):
        return True
    if isinstance(error, aiosmtplib.SMTPAuthenticationError):
        return True  # неверные учётные данные: повтор не поможет, нужно поправить настройки
    code = error.code if isinstance(error, aiosmtplib.SMTPResponseException) else None
    return code is not None and code >= 500


class SmtpMailer:
    def __init__(self, config: SmtpConfig, *, timeout: float = 10.0) -> None:
        self._config = config
        self._timeout = timeout

    async def send(self, message: EmailMessage) -> None:
        config = self._config
        try:
            await aiosmtplib.send(
                message,
                hostname=config.hostname,
                port=config.port,
                username=config.username,
                password=config.password,
                use_tls=config.use_tls,
                start_tls=config.start_tls,
                timeout=self._timeout,
            )
        except aiosmtplib.SMTPException as error:
            if _is_permanent(error):
                raise PermanentMailError(type(error).__name__) from error
            raise TransientMailError(type(error).__name__) from error
        except (OSError, TimeoutError) as error:
            raise TransientMailError(type(error).__name__) from error
