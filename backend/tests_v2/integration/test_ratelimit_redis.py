"""Token bucket на настоящем Redis: ёмкость, пополнение, возврат токена, конкуренция, скорость, сбой."""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import SecretStr
from redis.asyncio import Redis

from messunjerr.core.errors import DomainError
from messunjerr.core.ratelimit import BucketConfig, RateLimiter, UnavailablePolicy
from messunjerr.core.redis import create_redis
from messunjerr.settings import Settings


def limiter_for(
    redis: Redis, limit: int, window: int, *, policy: UnavailablePolicy = "allow"
) -> RateLimiter:
    return RateLimiter(redis, {"b": BucketConfig("b", limit, window, policy)})


async def test_the_bucket_lets_the_limit_through_and_then_refuses(redis_client: Redis) -> None:
    limiter = limiter_for(redis_client, limit=3, window=60)

    results = [await limiter.consume("b", "subject") for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert all(r.limit == 3 for r in results)
    denied = results[-1]
    assert 19 <= denied.retry_after <= 21  # токен возвращается раз в окно / лимит = 20 секунд
    assert denied.reset >= denied.retry_after
    assert denied.headers()["RateLimit-Remaining"] == "0"


async def test_the_bucket_refills_over_time(redis_client: Redis) -> None:
    limiter = limiter_for(redis_client, limit=2, window=1)  # два токена в секунду

    assert (await limiter.consume("b", "s")).allowed
    assert (await limiter.consume("b", "s")).allowed
    assert not (await limiter.consume("b", "s")).allowed

    await asyncio.sleep(0.7)  # за это время вернулся примерно один токен

    assert (await limiter.consume("b", "s")).allowed
    assert not (await limiter.consume("b", "s")).allowed


async def test_a_refund_returns_the_token_but_never_beyond_the_capacity(
    redis_client: Redis,
) -> None:
    limiter = limiter_for(redis_client, limit=3, window=3600)
    for _ in range(3):
        await limiter.consume("b", "s")
    assert not (await limiter.consume("b", "s")).allowed

    await limiter.refund("b", "s")
    assert (await limiter.consume("b", "s")).allowed

    for _ in range(10):  # лишние возвраты потолок не поднимают
        await limiter.refund("b", "s")
    assert (await limiter.consume("b", "s")).remaining == 2


async def test_a_request_can_cost_more_than_one_token(redis_client: Redis) -> None:
    limiter = limiter_for(redis_client, limit=5, window=50)

    first = await limiter.consume("b", "s", cost=3)
    second = await limiter.consume("b", "s", cost=3)

    assert (first.allowed, first.remaining) == (True, 2)
    assert second.allowed is False
    assert 9 <= second.retry_after <= 11  # не хватает одного токена, токен раз в 10 секунд


async def test_subjects_and_buckets_are_independent(redis_client: Redis) -> None:
    limiter = RateLimiter(
        redis_client,
        {"a": BucketConfig("a", 1, 60), "b": BucketConfig("b", 1, 60)},
    )

    assert (await limiter.consume("a", "x")).allowed
    assert not (await limiter.consume("a", "x")).allowed
    assert (await limiter.consume("a", "y")).allowed  # другой субъект
    assert (await limiter.consume("b", "x")).allowed  # другой бакет


async def test_the_state_disappears_once_the_bucket_is_full_again(redis_client: Redis) -> None:
    limiter = limiter_for(redis_client, limit=2, window=1)
    await limiter.consume("b", "s")
    await limiter.consume("b", "s")

    ttl = await redis_client.pttl("rl:b:s")
    assert 0 < ttl <= 3000

    await asyncio.sleep(3.2)
    assert await redis_client.exists("rl:b:s") == 0  # лишних ключей Redis не копит


async def test_parallel_requests_never_let_more_than_the_limit_through(redis_client: Redis) -> None:
    limiter = limiter_for(redis_client, limit=10, window=3600)

    results = await asyncio.gather(*(limiter.consume("b", "shared") for _ in range(200)))

    assert sum(r.allowed for r in results) == 10


async def test_ten_thousand_calls_are_counted_exactly_and_fast(redis_client: Redis) -> None:
    """Риск плана S2: Lua-скрипт должен быть верным и быстрым на потоке в десять тысяч вызовов."""
    limiter = limiter_for(redis_client, limit=10_000, window=86_400)
    started = time.perf_counter()

    allowed = 0
    for _ in range(20):
        batch = await asyncio.gather(*(limiter.consume("b", "bulk") for _ in range(500)))
        allowed += sum(r.allowed for r in batch)
    elapsed = time.perf_counter() - started

    assert allowed == 10_000
    assert not (await limiter.consume("b", "bulk")).allowed
    assert elapsed < 15  # на локальном Redis около секунды; запас на медленный CI


async def test_unknown_bucket_is_a_programming_error(redis_client: Redis) -> None:
    limiter = limiter_for(redis_client, limit=1, window=1)

    with pytest.raises(KeyError, match="не описан"):
        await limiter.consume("nope", "s")


async def test_overrides_change_only_the_named_limits(redis_client: Redis) -> None:
    base = RateLimiter(redis_client, {"a": BucketConfig("a", 5, 60), "b": BucketConfig("b", 7, 60)})

    changed = base.with_overrides(a=2)

    assert (changed.bucket("a").limit, changed.bucket("b").limit) == (2, 7)
    assert base.bucket("a").limit == 5


# ----------------------------------------------------------------------------- сбой Redis
@pytest_asyncio.fixture
async def dead_redis(test_settings: Settings) -> AsyncIterator[Redis]:
    """Клиент Redis, который никуда не подключится: порт 1 закрыт."""
    settings = test_settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    client = create_redis(settings)
    yield client
    await client.aclose()


async def test_an_open_bucket_lets_requests_through_when_redis_is_down(dead_redis: Redis) -> None:
    limiter = limiter_for(dead_redis, limit=3, window=60, policy="allow")

    result = await limiter.consume("b", "s")

    assert result.allowed
    assert result.remaining == 3


async def test_a_closed_bucket_answers_503_when_redis_is_down(dead_redis: Redis) -> None:
    limiter = limiter_for(dead_redis, limit=3, window=60, policy="deny")

    with pytest.raises(DomainError) as caught:
        await limiter.consume("b", "s")

    assert caught.value.status == 503
    assert caught.value.headers["Retry-After"] == "5"


async def test_a_refund_never_fails_the_request_even_without_redis(dead_redis: Redis) -> None:
    limiter = limiter_for(dead_redis, limit=3, window=60, policy="deny")

    await limiter.refund("b", "s")  # не бросает: вход уже подтверждён


async def test_a_disabled_limiter_does_not_touch_redis(dead_redis: Redis) -> None:
    limiter = RateLimiter(dead_redis, {"b": BucketConfig("b", 1, 60, "deny")}, enabled=False)

    results = [await limiter.consume("b", "s") for _ in range(5)]

    assert all(r.allowed for r in results)
