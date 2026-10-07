"""Доступ к Alembic из приложения: путь к конфигурации и ожидаемая ревизия `head`."""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

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


@lru_cache(maxsize=4)
def _known_revisions(ini_path: str) -> frozenset[str]:
    script = ScriptDirectory.from_config(alembic_config(Path(ini_path)))
    return frozenset(item.revision for item in script.walk_revisions())


def is_known_revision(revision: str, ini_path: Path | None = None) -> bool:
    """Знает ли код эту ревизию. Нет, если БД проведена более новым релизом (БД «впереди» кода)."""
    path = ini_path or alembic_ini_path()
    return path.exists() and revision in _known_revisions(str(path))


async def database_is_ahead(database_url: str, ini_path: Path | None = None) -> bool:
    """БД проведена более новым релизом: её ревизии нет среди известных этому коду.

    Так выглядит откат кода после выкладки с миграцией (S4): `alembic upgrade head` тут падает с
    «Can't locate revision», хотя делать нечего, и команда `migrate` обязана это пропустить, иначе
    одноразовая задача Compose не завершится и сервисы за ней не поднимутся. Новая БД (таблицы
    `alembic_version` ещё нет) «впереди» не считается.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
    except ProgrammingError:  # таблицы версий нет: база пустая, миграции нужны
        return False
    finally:
        await engine.dispose()
    return len(rows) == 1 and not is_known_revision(str(rows[0][0]), ini_path)
