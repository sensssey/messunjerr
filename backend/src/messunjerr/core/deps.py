"""Ресурсы приложения и зависимости FastAPI.

Ресурсы (движок БД, Redis, настройки) создаёт lifespan и кладёт в `app.state.resources`;
обработчики получают их через зависимости, а не через глобальные переменные.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.jobs import JobQueue
from messunjerr.core.uow import UnitOfWork
from messunjerr.settings import Settings


@dataclass(slots=True)
class AppResources:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    jobs: JobQueue
    expected_head: str | None


def get_resources(request: Request) -> AppResources:
    return cast(AppResources, request.app.state.resources)  # pyright: ignore[reportUnknownMemberType]


ResourcesDep = Annotated[AppResources, Depends(get_resources)]


def get_settings_dep(resources: ResourcesDep) -> Settings:
    return resources.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def get_uow(resources: ResourcesDep) -> AsyncIterator[UnitOfWork]:
    """Unit of Work на запрос: транзакцию фиксирует обработчик команды явным `commit()`."""
    async with UnitOfWork(resources.sessionmaker) as uow:
        yield uow


UowDep = Annotated[UnitOfWork, Depends(get_uow)]
