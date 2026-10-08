"""Регистрация таблиц всех контекстов в `Base.metadata` для процессов, которые собирают приложение не целиком.

Внешние ключи между схемами (`media.assets.owner_id` → `identity.users`, `profile.profiles.avatar_asset_id`
→ `media.assets`) SQLAlchemy разрешает по метаданным: пока модуль таблицы не импортирован, запись в
зависимую таблицу падает с `NoReferencedTableError`. API и воркеры импортируют всё, что им нужно,
сами, а команды (`seed`, `create-admin`, `reprocess-media`) и Alembic зовут `register_tables()`.
Каждый новый контекст добавляется сюда один раз.
"""

from importlib import import_module

MODEL_MODULES = (
    "messunjerr.core.models",
    "messunjerr.identity.infra.models",
    "messunjerr.profiles.infra.models",
    "messunjerr.media.infra.models",
)


def register_tables() -> None:
    for module in MODEL_MODULES:
        import_module(module)
