"""Лимиты запросов (4.14): token bucket в Redis, одна проверка = один вызов Lua-скрипта.

Бакет держит до `limit` токенов и пополняется со скоростью `limit / window`. Запрос забирает токен;
пока токены есть, он проходит, иначе получает `429` и `Retry-After` (секунды до ближайшего токена).
Состояние бакета (токены и время) хранится в hash `rl:{бакет}:{субъект}` и сам исчезает, когда бакет
снова полон. Время берётся у Redis (`TIME` в скрипте), а не у приложения: часы нескольких экземпляров
API не расходятся.
"""

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from messunjerr.core.codes import ErrorCode
from messunjerr.core.errors import DomainError
from messunjerr.core.logs import get_logger

UnavailablePolicy = Literal["allow", "deny"]

_WINDOW = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86_400}
_UNAVAILABLE_RETRY_AFTER = "5"

# KEYS[1]: ключ; ARGV: ёмкость, токенов в секунду, стоимость, режим ("consume" или "refund").
# Возвращает {разрешено (0/1), токенов осталось (целое), мс до ближайшего токена, мс до полного бакета}.
_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local refund = ARGV[4] == 'refund'

local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local stamp = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  stamp = now
end
tokens = math.min(capacity, tokens + math.max(0, now - stamp) / 1000 * rate)

local allowed = 0
if refund then
  tokens = math.min(capacity, tokens + cost)
  allowed = 1
elseif tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

redis.call('HSET', KEYS[1], 'tokens', string.format('%.6f', tokens), 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil((capacity - tokens) / rate * 1000) + 1000)

local retry_ms = 0
if allowed == 0 then
  retry_ms = math.ceil((cost - tokens) / rate * 1000)
end
local reset_ms = math.ceil((capacity - tokens) / rate * 1000)
return {allowed, math.floor(tokens), retry_ms, reset_ms}
"""


def parse_window(value: str) -> int:
    """`"10m"` -> 600 секунд."""
    found = _WINDOW.match(value.strip())
    if found is None or int(found.group(1)) < 1:
        raise ValueError(
            f"окно лимита {value!r}: ожидается число и единица, например 30s, 10m, 1h, 1d"
        )
    return int(found.group(1)) * _UNIT_SECONDS[found.group(2)]


@dataclass(frozen=True, slots=True)
class BucketConfig:
    name: str
    limit: int
    window_seconds: int
    on_unavailable: UnavailablePolicy = "allow"

    @property
    def refill_per_second(self) -> float:
        return self.limit / self.window_seconds


def parse_buckets(raw: Mapping[str, Any]) -> dict[str, BucketConfig]:
    buckets: dict[str, BucketConfig] = {}
    for name, table in raw.items():
        if not isinstance(table, dict):
            raise ValueError(f"лимиты: [{name}] должен быть таблицей")
        fields: dict[str, Any] = dict(table)  # pyright: ignore[reportUnknownArgumentType]
        unknown = set(fields) - {"limit", "window", "on_unavailable"}
        if unknown:
            raise ValueError(f"лимиты: в [{name}] неизвестные поля {sorted(unknown)}")
        limit = fields.get("limit")
        window = fields.get("window")
        policy = fields.get("on_unavailable", "allow")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"лимиты: в [{name}] limit должен быть целым числом не меньше 1")
        if not isinstance(window, str):
            raise ValueError(f"лимиты: в [{name}] не задано window")
        if policy not in ("allow", "deny"):
            raise ValueError(f"лимиты: в [{name}] on_unavailable это allow или deny")
        buckets[name] = BucketConfig(name, limit, parse_window(window), policy)
    return buckets


def load_buckets(override_file: Path | None = None) -> dict[str, BucketConfig]:
    """Значения по умолчанию из пакета, поверх них (по полям) файл оператора."""
    defaults = resources.files("messunjerr.core").joinpath("ratelimits.toml").read_text("utf-8")
    merged: dict[str, dict[str, Any]] = {
        name: dict(table)  # pyright: ignore[reportUnknownArgumentType]
        for name, table in tomllib.loads(defaults).items()
    }
    if override_file is not None:
        overrides = tomllib.loads(override_file.read_text(encoding="utf-8"))
        for name, table in overrides.items():
            if not isinstance(table, dict):
                raise ValueError(f"лимиты: [{name}] должен быть таблицей")
            merged.setdefault(name, {}).update(table)  # pyright: ignore[reportUnknownArgumentType]
    return parse_buckets(merged)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    bucket: str
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    """Секунды до появления токена; 0, если запрос разрешён."""
    reset: int
    """Секунды до полного восстановления бакета."""

    def headers(self) -> dict[str, str]:
        return {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset),
        }


def _unlimited(config: BucketConfig) -> RateLimitResult:
    """Ответ «ограничений нет»: лимиты выключены или Redis недоступен, а бакет пропускает запросы."""
    return RateLimitResult(
        bucket=config.name,
        allowed=True,
        limit=config.limit,
        remaining=config.limit,
        retry_after=0,
        reset=0,
    )


class RateLimiter:
    def __init__(
        self, redis: Redis, buckets: Mapping[str, BucketConfig], *, enabled: bool = True
    ) -> None:
        self._redis = redis
        self._buckets = dict(buckets)
        self._enabled = enabled
        self._script = redis.register_script(_BUCKET_SCRIPT)  # pyright: ignore[reportUnknownMemberType]
        self._log = get_logger("messunjerr.ratelimit")

    def bucket(self, name: str) -> BucketConfig:
        try:
            return self._buckets[name]
        except KeyError:
            raise KeyError(f"бакет лимитов {name!r} не описан в ratelimits.toml") from None

    def with_overrides(self, **limits: int) -> "RateLimiter":
        """Копия с другими `limit` у перечисленных бакетов (удобно в тестах)."""
        changed = {
            name: replace(config, limit=limits.get(name, config.limit))
            for name, config in self._buckets.items()
        }
        return RateLimiter(self._redis, changed, enabled=self._enabled)

    async def consume(self, bucket: str, subject: str, cost: int = 1) -> RateLimitResult:
        """Забирает токен. Недоступный Redis: пропустить или `503`, как задано у бакета."""
        return await self._run(bucket, subject, cost, "consume")

    async def refund(self, bucket: str, subject: str, cost: int = 1) -> None:
        """Возвращает токен: попытка входа оказалась успешной и не должна считаться перебором.

        Лучшее усилие: сбой Redis на этом шаге не должен ломать уже подтверждённый вход.
        """
        try:
            await self._run(bucket, subject, cost, "refund")
        except DomainError:
            self._log.warning("ratelimit_refund_failed", bucket=bucket)

    async def _run(self, bucket: str, subject: str, cost: int, mode: str) -> RateLimitResult:
        config = self.bucket(bucket)
        if not self._enabled:
            return _unlimited(config)
        try:
            raw = await self._script(  # pyright: ignore[reportUnknownVariableType]
                keys=[f"rl:{bucket}:{subject}"],
                args=[config.limit, config.refill_per_second, cost, mode],
            )
            allowed, remaining, retry_ms, reset_ms = (int(value) for value in raw)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        except (RedisError, OSError, TimeoutError) as error:
            if config.on_unavailable == "deny":
                raise DomainError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    "The rate limiter is unavailable.",
                    headers={"Retry-After": _UNAVAILABLE_RETRY_AFTER},
                ) from error
            self._log.warning(
                "ratelimit_unavailable", bucket=bucket, error_type=type(error).__name__
            )
            return _unlimited(config)
        return RateLimitResult(
            bucket=bucket,
            allowed=bool(allowed),
            limit=config.limit,
            remaining=remaining,
            retry_after=math.ceil(retry_ms / 1000) if not allowed else 0,
            reset=math.ceil(reset_ms / 1000),
        )


def rate_limited(result: RateLimitResult) -> DomainError:
    """`429 rate_limited`: `Retry-After`, `RateLimit-*` и поле `retry_after` в теле (5.1)."""
    retry_after = max(1, result.retry_after)
    return DomainError(
        ErrorCode.RATE_LIMITED,
        headers={**result.headers(), "Retry-After": str(retry_after)},
        retry_after=retry_after,
    )


def most_restrictive(results: list[RateLimitResult]) -> RateLimitResult:
    """Из нескольких бакетов запроса показываем тот, где запас меньше всего."""
    return min(results, key=lambda result: result.remaining / result.limit)
