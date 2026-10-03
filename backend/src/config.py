import logging
import os

from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Переменная окружения {name} не задана (см. .env.example)")
    return value


SECRET_KEY = _require("SECRET_KEY")
if SECRET_KEY.startswith("change-me"):
    logger.warning("SECRET_KEY всё ещё равен значению-заглушке из .env.example, задайте свой ключ")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))

# Строку подключения можно задать целиком (удобно для тестов), иначе она собирается из POSTGRES_*
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = URL.create(
        "postgresql+asyncpg",
        username=_require("POSTGRES_USER"),
        password=_require("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        database=_require("POSTGRES_DB"),
    ).render_as_string(hide_password=False)

# Логирование всех SQL-запросов: только для отладки
SQL_ECHO = os.getenv("SQL_ECHO", "").lower() in ("1", "true", "yes")

# Адреса фронтенда, которым разрешено ходить в API из браузера (через запятую)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:1337,http://127.0.0.1:1337,http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]

AVATAR_MAX_BYTES = int(os.getenv("AVATAR_MAX_BYTES", 5 * 1024 * 1024))
