"""Почта: порт `Mailer`, сборка писем из шаблонов и отправка по SMTP."""

from messunjerr.core.mail.base import (
    InMemoryMailer,
    Mailer,
    MailError,
    PermanentMailError,
    TransientMailError,
)
from messunjerr.core.mail.render import APP_NAME, render_email
from messunjerr.core.mail.smtp import SmtpConfig, SmtpMailer, parse_smtp_url

__all__ = [
    "APP_NAME",
    "InMemoryMailer",
    "MailError",
    "Mailer",
    "PermanentMailError",
    "SmtpConfig",
    "SmtpMailer",
    "TransientMailError",
    "parse_smtp_url",
    "render_email",
]
