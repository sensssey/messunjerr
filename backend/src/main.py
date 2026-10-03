import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.cors import CORSMiddleware

from src.config import CORS_ORIGINS
from src.database import engine, Base, get_db
from src.auth.routes import auth_router
from src.posts.routes import posts_router
from src.profile.routes import profile_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DB_CONNECT_ATTEMPTS = 30
DB_CONNECT_DELAY = 1.0


async def init_db():
    """Создаёт таблицы. Postgres может ещё запускаться, поэтому пробуем несколько раз и падаем, если БД так и не ответила."""
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database connection established")
            return
        except Exception as e:
            if attempt == DB_CONNECT_ATTEMPTS:
                logger.error("Database connection failed: %s", e)
                raise
            logger.warning("Database is not ready (attempt %s/%s): %s", attempt, DB_CONNECT_ATTEMPTS, e)
            await asyncio.sleep(DB_CONNECT_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(
    title="messunjerr API",
    description="Бэкенд мессунжера: аутентификация, посты и аватары пользователей.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(posts_router)
app.include_router(profile_router)


@app.get("/health", tags=["Service"])
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check failed")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
