"""Unit of Work: одна транзакция на команду, состояние и события фиксируются атомарно (4.3).

Репозитории `commit()` не вызывают. После успешной фиксации выполняются after-commit hooks
(публикация в Redis Pub/Sub и т.п.): это best-effort, их сбой не откатывает транзакцию.
"""

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.logs import get_logger
from messunjerr.core.outbox import Outbox

AfterCommitHook = Callable[[], Awaitable[object]]


class UnitOfWork:
    session: AsyncSession
    outbox: Outbox

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self._hooks: list[AfterCommitHook] = []
        self._log = get_logger("messunjerr.uow")

    async def __aenter__(self) -> Self:
        self.session = self._sessionmaker()
        await self.session.begin()
        self.outbox = Outbox(self.session)
        self._hooks = []
        return self

    def after_commit(self, hook: AfterCommitHook) -> None:
        """Регистрирует действие, которое выполнится только после успешного `commit()`."""
        self._hooks.append(hook)

    async def commit(self) -> None:
        await self.session.commit()
        hooks, self._hooks = self._hooks, []
        for hook in hooks:
            try:
                await hook()
            except Exception:
                # Состояние уже зафиксировано: клиент догрузит пропущенное по курсору.
                self._log.warning("after_commit_failed", exc_info=True)

    async def rollback(self) -> None:
        self._hooks = []
        await self.session.rollback()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self.session.in_transaction():
                await self.session.rollback()
        finally:
            self._hooks = []
            await self.session.close()
