"""Доступ к Alembic из приложения: путь к конфигурации и ожидаемая ревизия `head`."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# backend/src/messunjerr/core/migrations.py -> backend (в образе: /app)
BACKEND_DIR = Path(__file__).resolve().parents[3]


def alembic_ini_path() -> Path:
    return BACKEND_DIR / "alembic.ini"


def alembic_config(ini_path: Path | None = None) -> Config:
    return Config(str(ini_path or alembic_ini_path()))


def expected_head(ini_path: Path | None = None) -> str | None:
    """Ревизия, до которой должна быть доведена БД. `None`, если миграций нет рядом с кодом."""
    path = ini_path or alembic_ini_path()
    if not path.exists():
        return None
    return ScriptDirectory.from_config(alembic_config(path)).get_current_head()
