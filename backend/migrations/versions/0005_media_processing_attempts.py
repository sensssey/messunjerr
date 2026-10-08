"""media: счётчик обрывов разбора файла (защита от «ядовитых» файлов)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Выкладка без простоя (4.16): столбец с постоянным значением по умолчанию PostgreSQL добавляет без
    # перезаписи таблицы, остаётся лишь короткая блокировка каталога; ждать её дольше 10 секунд незачем.
    # Старые реплики столбца не знают и не пишут в него: значение берётся по умолчанию.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "assets",
        sa.Column(
            "processing_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        schema="media",
    )


def downgrade() -> None:
    op.drop_column("assets", "processing_attempts", schema="media")
