"""search: триграммные и префиксный индексы для поиска людей (S8-04)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Расширения `pg_trgm` и `citext` создала миграция 0001. Права на индексы отдельной выдачи не требуют.
INDEXES = (
    "ix_users_username_trgm",
    "ix_users_username_prefix",
    "ix_profiles_display_name_trgm",
)


def _require_cyrillic_trigrams() -> None:
    """`pg_trgm` не строит триграммы по не-ASCII буквам, если `LC_CTYPE` базы равен `C` (в том числе при
    ICU-сортировке): поиск по русским именам молча ничего бы не находил. Лучше остановить миграцию."""
    count = op.get_bind().execute(sa.text("SELECT cardinality(show_trgm('Анна'))")).scalar_one()
    if count == 0:
        raise RuntimeError(
            "Поиск людей требует базы с LC_CTYPE, где работают кириллические буквы: у этой базы "
            "pg_trgm не строит триграммы по русскому тексту (LC_CTYPE=C). Создайте базу командой "
            "CREATE DATABASE <имя> OWNER migrator TEMPLATE template0 LOCALE_PROVIDER builtin "
            "LOCALE 'C.UTF-8' (или LC_CTYPE 'en_US.utf8') и перенесите данные."
        )


def upgrade() -> None:
    _require_cyrillic_trigrams()
    # Ник всегда в нижнем регистре (CHECK username_format), поэтому `username::text` без lower().
    # Точное совпадение и префикс (LIKE 'abc%') идут по btree с text_pattern_ops, похожие ники и слова
    # внутри ника (`ivan_petrov` по запросу `petrov`) по GIN.
    op.execute(
        "CREATE INDEX ix_users_username_trgm ON identity.users "
        "USING gin ((username::text) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_users_username_prefix ON identity.users ((username::text) text_pattern_ops)"
    )
    # «ё» и «е» считаются одной буквой: имя и запрос приводятся одной и той же `translate`.
    # Регистр триграммы снимают сами. Выражение запроса и индекса обязано совпадать буква в букву.
    op.execute(
        "CREATE INDEX ix_profiles_display_name_trgm ON profile.profiles "
        "USING gin ((translate(display_name, 'ёЁ', 'еЕ')) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX profile.ix_profiles_display_name_trgm")
    op.execute("DROP INDEX identity.ix_users_username_prefix")
    op.execute("DROP INDEX identity.ix_users_username_trgm")
