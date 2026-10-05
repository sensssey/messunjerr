"""Непрозрачные токены (refresh, письма): в БД хранится только SHA-256, сам токен знает владелец.

Токен берётся из 256 бит криптографически стойких случайных данных. Для таких значений быстрого
SHA-256 достаточно: перебор невозможен, соль и медленный хэш (как у паролей) не нужны.
"""

import hashlib
import secrets

TOKEN_BYTES = 32


def new_opaque_token() -> str:
    """Новый токен: 43 символа base64url без заполнителя."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    """Значение для столбцов `*_hash` (bytea, 32 байта)."""
    return hashlib.sha256(token.encode("utf-8")).digest()
