"""Правила ников (4.5, 5.2): 3–30 символов `a–z 0–9 _`, регистр игнорируется, хранится в нижнем.

Чистые функции без обращений к БД. Занятость ника проверяет команда или запрос.
"""

import re
import uuid
from enum import StrEnum

USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,30}$")
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 30
_UUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

# Ники, которые заняты под адреса, роли и сервисные страницы: человек получить их не может.
RESERVED_USERNAMES: frozenset[str] = frozenset(
    {
        "about", "abuse", "account", "accounts", "admin", "administrator", "api", "app", "auth",
        "billing", "blog", "chat", "chats", "comment", "comments", "contact", "dashboard",
        "deleted", "dev", "docs", "email", "explore", "faq", "feed", "friends", "help", "home",
        "hostmaster", "info", "legal", "login", "logout", "mail", "media", "messages",
        "messunjerr", "moderator", "mod", "news", "noreply", "no_reply", "notifications", "null",
        "official", "onboarding", "post", "posts", "postmaster", "privacy", "profile", "register",
        "registration", "reports", "root", "search", "security", "settings", "signin", "signup",
        "staff", "status", "support", "system", "team", "terms", "test", "undefined", "user",
        "users", "webmaster", "welcome", "www",
    }
)  # fmt: skip


class UsernameProblem(StrEnum):
    INVALID = "invalid"
    RESERVED = "reserved"


def normalize_username(raw: str) -> str:
    """Приводит ввод к виду хранения: края обрезаны, регистр нижний."""
    return raw.strip().lower()


def check_username(username: str) -> UsernameProblem | None:
    """Проверяет уже нормализованный ник; `None` означает «подходит» (занятость не проверяется)."""
    if not USERNAME_PATTERN.fullmatch(username):
        return UsernameProblem.INVALID
    if username in RESERVED_USERNAMES:
        return UsernameProblem.RESERVED
    return None


def parse_user_ref(ref: str) -> uuid.UUID | str | None:
    """Разбирает `{ref}` из `/users/{ref}` (5.3): UUID в каноническом виде или ник.

    Ник не совпадает по форме с UUID (в нём нет дефисов), поэтому неоднозначности нет. Всё, что не
    похоже ни на то, ни на другое, даёт `None`: для клиента это «такого нет», а не ошибка формата.
    """
    if _UUID_TEXT.fullmatch(ref):
        return uuid.UUID(ref)
    username = normalize_username(ref)
    return username if USERNAME_PATTERN.fullmatch(username) else None
