"""Сборка писем из шаблонов Jinja2 (на русском). На каждое письмо три файла в `templates/`:

- `<имя>.subject.j2`: тема в одну строку;
- `<имя>.text.j2`: текстовая часть (без экранирования HTML);
- `<имя>.html.j2`: HTML-часть (с автоматическим экранированием; общая обёртка `_base.html.j2`).

Любая неопределённая переменная в шаблоне считается ошибкой (`StrictUndefined`), чтобы опечатка
не превратилась в письмо с пустым местом.
"""

from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from functools import cache
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

APP_NAME = "messunjerr"


@cache
def _html_environment() -> Environment:
    return Environment(
        loader=PackageLoader("messunjerr.core.mail", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@cache
def _plain_environment() -> Environment:
    # Тема и текстовая часть письма не HTML: экранирование превратило бы `&` в ссылках в `&amp;`.
    return Environment(
        loader=PackageLoader("messunjerr.core.mail", "templates"),
        autoescape=False,  # noqa: S701
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _domain_of(sender: str) -> str:
    return parseaddr(sender)[1].rpartition("@")[2] or "localhost"


def render_email(
    template: str, context: Mapping[str, Any], *, sender: str, to: str
) -> EmailMessage:
    """Собирает письмо `template` для `to`. Переносы строк в заголовках запрещает сам `EmailMessage`."""
    variables: dict[str, Any] = {"app_name": APP_NAME, **context}
    plain = _plain_environment()
    subject = plain.get_template(f"{template}.subject.j2").render(variables)
    text = plain.get_template(f"{template}.text.j2").render(variables)
    html = _html_environment().get_template(f"{template}.html.j2").render(variables)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = " ".join(subject.split())
    message["Date"] = formatdate()
    message["Message-ID"] = make_msgid(domain=_domain_of(sender))
    # RFC 3834: автоматическое письмо; не должно вызывать автоответов.
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message
