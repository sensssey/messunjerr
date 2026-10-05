"""Идемпотентность создающих `POST` (5.1): заголовок `Idempotency-Key`, ответ хранится 24 часа.

Повтор с тем же ключом и тем же телом возвращает исходный ответ (`Idempotency-Replayed: true`);
тот же ключ с другим телом даёт `422 idempotency_key_reuse`; пока первый запрос выполняется,
повтор получает `409 request_in_progress`.

Как устроено. Незавершённый запрос охраняет замок в Redis `idem:lock:{user}:{key}` на 60 секунд
(в значении лежит хэш запроса, чтобы отличить повтор от подмены тела). Результат (статус и тело
успешного ответа) после выполнения пишется в `platform.idempotency_keys`. Если Redis недоступен,
запрос выполняется без замка (в журнал идёт предупреждение): параллельный дубль возможен, но
последовательный повтор по-прежнему получит сохранённый ответ.

Подключение: `APIRouter(route_class=IdempotentRoute)`, где класс собран `idempotent_route_class`
с функцией, определяющей пользователя по запросу. Ключ действует в пределах пользователя.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ErrorCode, ItemCode
from messunjerr.core.deps import AppResources
from messunjerr.core.errors import DomainError, ErrorItem
from messunjerr.core.logs import get_logger
from messunjerr.core.models import IdempotencyKeyRow
from messunjerr.core.uow import UnitOfWork

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"
LOCK_TTL_SECONDS = 60
_STORED_HEADERS = ("location",)

SubjectResolver = Callable[[Request], Awaitable[uuid.UUID | None]]
RouteHandler = Callable[[Request], Coroutine[Any, Any, Response]]


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: Any
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Record:
    """Запись из `platform.idempotency_keys`, снятая до закрытия сессии."""

    request_hash: bytes
    response: StoredResponse


def request_fingerprint(request: Request, body: bytes) -> bytes:
    """Хэш метода, пути, запроса и тела. JSON приводится к каноническому виду: пробелы и порядок
    ключей не превращают повтор в «другое тело»."""
    try:
        canonical = json.dumps(
            json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (ValueError, UnicodeDecodeError):
        canonical = body
    query = "&".join(sorted(request.url.query.split("&"))) if request.url.query else ""
    material = b"\n".join(
        [request.method.encode(), request.url.path.encode(), query.encode(), canonical]
    )
    return hashlib.sha256(material).digest()


def _lock_key(user_id: uuid.UUID, key: str) -> str:
    return f"idem:lock:{user_id}:{key}"


def parse_key(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            errors=[
                ErrorItem(
                    f"/header/{HEADER}",
                    ItemCode.INVALID_FORMAT,
                    "The Idempotency-Key must be a UUID.",
                )
            ],
        ) from None


def _reuse() -> DomainError:
    return DomainError(
        ErrorCode.IDEMPOTENCY_KEY_REUSE,
        "This Idempotency-Key was already used with a different request.",
    )


class IdempotencyStore:
    def __init__(self, resources: AppResources) -> None:
        self._resources = resources
        self._log = get_logger("messunjerr.idempotency")

    async def _stored(self, user_id: uuid.UUID, key: str) -> _Record | None:
        async with UnitOfWork(self._resources.sessionmaker) as uow:
            row = await uow.session.get(IdempotencyKeyRow, (user_id, key))
            if row is None or row.expires_at <= utcnow():
                return None
            envelope: dict[str, Any] = row.response_body or {}
            return _Record(
                request_hash=bytes(row.request_hash),
                response=StoredResponse(
                    status=row.response_status or 200,
                    body=envelope.get("body"),
                    headers=dict(envelope.get("headers", {})),
                ),
            )

    @staticmethod
    def _replay(record: _Record, fingerprint: bytes) -> StoredResponse:
        if record.request_hash != fingerprint:
            raise _reuse()
        return record.response

    async def begin(
        self, user_id: uuid.UUID, key: str, fingerprint: bytes
    ) -> StoredResponse | None:
        """Сохранённый ответ для воспроизведения или `None`, если запрос нужно выполнить."""
        record = await self._stored(user_id, key)
        if record is not None:
            return self._replay(record, fingerprint)

        try:
            acquired = await self._resources.redis.set(  # pyright: ignore[reportUnknownMemberType]
                _lock_key(user_id, key), fingerprint.hex(), nx=True, ex=LOCK_TTL_SECONDS
            )
            if not acquired:
                holder = await self._resources.redis.get(_lock_key(user_id, key))  # pyright: ignore[reportUnknownMemberType]
                if holder is not None and holder != fingerprint.hex():
                    raise _reuse()
                raise DomainError(
                    ErrorCode.REQUEST_IN_PROGRESS,
                    "The first request with this Idempotency-Key is still being processed.",
                    headers={"Retry-After": "1"},
                )
        except (RedisError, OSError, TimeoutError):
            self._log.warning("idempotency_lock_unavailable")
            return None

        # Пока брали замок, первый запрос мог завершиться: смотрим ещё раз.
        record = await self._stored(user_id, key)
        if record is not None:
            await self.release(user_id, key)
            return self._replay(record, fingerprint)
        return None

    async def release(self, user_id: uuid.UUID, key: str) -> None:
        try:
            await self._resources.redis.delete(_lock_key(user_id, key))  # pyright: ignore[reportUnknownMemberType]
        except (RedisError, OSError, TimeoutError):
            self._log.warning("idempotency_unlock_failed")

    async def complete(
        self, user_id: uuid.UUID, key: str, fingerprint: bytes, response: Response
    ) -> None:
        """Сохраняет успешный ответ (2xx с JSON или пустым телом) и снимает замок."""
        try:
            stored = self._storable(response)
            if stored is not None:
                async with UnitOfWork(self._resources.sessionmaker) as uow:
                    expires = utcnow() + timedelta(
                        hours=self._resources.settings.idempotency_ttl_hours
                    )
                    statement = insert(IdempotencyKeyRow).values(
                        user_id=user_id,
                        key=key,
                        request_hash=fingerprint,
                        response_status=response.status_code,
                        response_body={"body": stored[0], "headers": stored[1]},
                        expires_at=expires,
                    )
                    # Ключ, чья запись уже истекла, но ещё не вычищена, можно использовать снова.
                    await uow.session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[IdempotencyKeyRow.user_id, IdempotencyKeyRow.key],
                            set_={
                                "request_hash": statement.excluded.request_hash,
                                "response_status": statement.excluded.response_status,
                                "response_body": statement.excluded.response_body,
                                "created_at": func.now(),
                                "expires_at": statement.excluded.expires_at,
                            },
                            where=IdempotencyKeyRow.expires_at <= func.now(),
                        )
                    )
                    await uow.commit()
        finally:
            await self.release(user_id, key)

    @staticmethod
    def _storable(response: Response) -> tuple[Any, dict[str, str]] | None:
        if not 200 <= response.status_code < 300:
            return None
        body: bytes | None = getattr(response, "body", None)  # у потоковых ответов тела нет
        if body is None:
            return None
        headers = {
            name: response.headers[name] for name in _STORED_HEADERS if name in response.headers
        }
        if not body:
            return None, headers
        if "json" not in response.headers.get("content-type", ""):
            return None
        return json.loads(body), headers


def idempotent_route_class(subject: SubjectResolver) -> type[APIRoute]:
    """Класс маршрута, который обслуживает `Idempotency-Key` на `POST`.

    `subject` определяет пользователя по запросу (`None`: не вошёл, идемпотентность не нужна, а
    401 выдаст сама ручка). Заголовок необязателен: без него запрос выполняется как обычно.
    """

    class IdempotentRoute(APIRoute):
        def get_route_handler(self) -> RouteHandler:
            original = super().get_route_handler()

            async def handler(request: Request) -> Response:
                raw_key = request.headers.get(HEADER)
                if raw_key is None or request.method != "POST":
                    return await original(request)
                user_id = await subject(request)
                if user_id is None:
                    return await original(request)  # 401 выдаст сама ручка: аутентификация первична
                key = parse_key(raw_key)

                resources: AppResources = request.app.state.resources
                store = IdempotencyStore(resources)
                fingerprint = request_fingerprint(request, await request.body())
                stored = await store.begin(user_id, key, fingerprint)
                if stored is not None:
                    return _replayed(stored)
                try:
                    response = await original(request)
                except BaseException:
                    await store.release(user_id, key)
                    raise
                await store.complete(user_id, key, fingerprint, response)
                return response

            return handler

    return IdempotentRoute


def _replayed(stored: StoredResponse) -> Response:
    headers = {**stored.headers, REPLAYED_HEADER: "true"}
    if stored.body is None:
        return Response(status_code=stored.status, headers=headers)
    return JSONResponse(stored.body, status_code=stored.status, headers=headers)


KEY_RETENTION_GRACE = timedelta(days=7)
"""Запись живёт до срока и ещё 7 дней (спецификация 6.9.7)."""


async def purge_expired_keys(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    grace: timedelta = KEY_RETENTION_GRACE,
    now: datetime | None = None,
    batch_size: int = 1000,
) -> int:
    """Удаляет записи, срок которых вышел больше `grace` назад (плановая очистка). Пачками: долгих
    транзакций нет.

    Запись и так игнорируется после срока (`begin` её не видит), очистка нужна ради размера таблицы.
    """
    cutoff = (now or utcnow()) - grace
    removed = 0
    while True:
        async with UnitOfWork(sessionmaker) as uow:
            old = aliased(IdempotencyKeyRow)
            stale = (
                select(old.user_id, old.key)
                .where(old.expires_at <= cutoff)
                .limit(batch_size)
                .with_for_update(skip_locked=True, of=old)
            )
            result = await uow.session.execute(
                delete(IdempotencyKeyRow)
                .where(tuple_(IdempotencyKeyRow.user_id, IdempotencyKeyRow.key).in_(stale))
                .returning(IdempotencyKeyRow.key)
                .execution_options(synchronize_session=False)
            )
            count = len(result.all())
            await uow.commit()
        removed += count
        if count < batch_size:
            return removed
