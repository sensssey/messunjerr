"""Поиск людей на 5 000 человек (приёмка S8): p95 меньше 100 мс, индексы используются, `seed-big` быстр.

Данные те же, что у `make seed-big` (тот же генератор, граф связей и блокировки), запросы идут через
HTTP-клиент приложения со всем, что в нём есть: токен, лимиты выключены, БД настоящая. Порог в тесте
с большим запасом (250 мс) против цели 100 мс: тест ловит порчу индекса или плана, а не шум машины;
настоящие цифры и планы лежат в заметках S8. Тест идёт около 30 секунд (создание базы и 5 000 человек),
поэтому включается переменной `SEARCH_PERF=1`.
"""

import json
import os
import random
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.core.shutdown import ShutdownGate
from messunjerr.main import create_app
from messunjerr.media.infra.memory import InMemoryObjectStorage
from messunjerr.seeding import SEED_PASSWORD, BigSeedResult, seed_big_database
from messunjerr.settings import Settings
from messunjerr.social.queries.search import SEARCH_SETTINGS, search_statement

from .conftest import DatabaseUnderTest, create_database, drop_database
from .helpers import bearer, fetch_all
from .search_helpers import SEARCH

pytestmark = pytest.mark.skipif(
    os.environ.get("SEARCH_PERF") != "1",
    reason="медленный замер 5 000 человек: включается SEARCH_PERF=1",
)

USERS = 5000
P95_LIMIT_MS = 250.0  # цель приёмки 100 мс; запас на загруженную машину
SEEDING_LIMIT_SECONDS = 120.0  # приёмка: 5 000 человек и все связи не дольше двух минут


@dataclass(frozen=True, slots=True)
class Big:
    database: DatabaseUnderTest
    settings: Settings
    seeded: BigSeedResult
    client: httpx.AsyncClient
    headers: dict[str, str]
    engine: AsyncEngine


@pytest_asyncio.fixture(scope="module")
async def big(base_settings: Settings, test_settings: Settings) -> AsyncIterator[Big]:
    target = await create_database(base_settings)
    settings = test_settings.model_copy(
        update={
            "database_url": SecretStr(target.app_url),
            "migrator_database_url": SecretStr(target.migrator_url),
            "admin_database_url": SecretStr(target.admin_url),
        }
    )
    seeded = await seed_big_database(settings, users=USERS)
    admin = create_async_engine(target.admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as connection:  # статистика планировщика, как после автоочистки
            await connection.exec_driver_sql("ANALYZE")
    finally:
        await admin.dispose()
    application = create_app(
        settings,
        job_queue=InMemoryJobQueue(),
        shutdown_gate=ShutdownGate(),
        storage=InMemoryObjectStorage(),
    )
    engine = create_async_engine(target.app_url, poolclass=NullPool)
    try:
        async with LifespanManager(application):
            transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                login = await http.post(
                    "/api/v1/auth/login", json={"login": "big_00001", "password": SEED_PASSWORD}
                )
                assert login.status_code == 200, login.text
                yield Big(
                    target, settings, seeded, http, bearer(login.json()["access_token"]), engine
                )
    finally:
        await engine.dispose()
        await drop_database(base_settings, target.name)


async def sample_names(big: Big) -> list[str]:
    rows = await fetch_all(big.engine, "SELECT display_name FROM profile.profiles ORDER BY user_id")
    return [row["display_name"] for row in rows]


def make_queries(names: list[str]) -> list[tuple[str, dict[str, int]]]:
    """Разные запросы: целые имена, слова наоборот, начала слов, опечатки, латиница, ники, пустые."""
    rng = random.Random(11)
    chosen = rng.sample(names, 80)
    queries: list[tuple[str, dict[str, int]]] = [(name, {}) for name in chosen[:40]]  # целые имена
    queries += [
        (" ".join(reversed(name.split())), {}) for name in chosen[40:60]
    ]  # слова наоборот (одно слово остаётся как есть)
    for name in chosen[:60]:
        word = max(name.replace(".", " ").split(), key=len)
        queries += [
            (word[:size], {"limit": rng.choice([20, 50])})
            for size in (2, 3, 4)
            if len(word) >= size
        ]
    for name in chosen[60:80]:
        word = max(name.split(), key=len)
        if len(word) >= 5:
            at = rng.randrange(1, len(word) - 2)
            queries.append((word[:at] + word[at + 1] + word[at] + word[at + 2 :], {}))  # опечатка
            queries.append((word[:at] + word[at + 1 :], {"offset": 20}))  # пропущена буква
    queries += [(q, {}) for q in ("john", "john smith", "smith j", "jo", "zoe muller", "emma")]
    queries += [(q, {}) for q in ("big_0", "big_00", "big_01234", "bi", "big_049", "00")]
    queries += [(q, {}) for q in ("йцукен", "zzzz", "ъъ", "фывапр", "--")]
    return queries


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * share + 0.999999) - 1)]


async def test_five_thousand_people_are_seeded_within_the_limit_and_search_p95_is_in_budget(
    big: Big,
) -> None:
    assert big.seeded.created == USERS
    assert big.seeded.seconds < SEEDING_LIMIT_SECONDS
    queries = make_queries(await sample_names(big))
    assert len(queries) >= 200
    for q, params in queries[:12]:  # прогрев: подготовленные запросы и кэш страниц
        await big.client.get(SEARCH, params={"q": q, **params}, headers=big.headers)

    timings: list[float] = []
    slowest: tuple[float, str] = (0.0, "")
    found = 0
    for q, params in queries:
        started = time.perf_counter()
        response = await big.client.get(SEARCH, params={"q": q, **params}, headers=big.headers)
        elapsed = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, (q, response.text)
        timings.append(elapsed)
        found += bool(response.json()["items"])
        slowest = max(slowest, (elapsed, q))

    p95 = percentile(timings, 0.95)
    print(  # noqa: T201 (цифры замера нужны тому, кто запускает тест с `-s`)
        f"\nпосев {USERS}: {big.seeded.seconds:.1f} с; запросов {len(timings)}, с результатом {found}; "
        f"p50 {statistics.median(timings):.1f} мс, p95 {p95:.1f} мс, "
        f"p99 {percentile(timings, 0.99):.1f} мс, max {slowest[0]:.1f} мс ({slowest[1]!r})"
    )
    assert found > len(timings) * 0.8  # запросы осмысленные: у большинства есть результат
    assert p95 < P95_LIMIT_MS, f"p95 {p95:.1f} мс"


async def plan_of(big: Big, query: str) -> str:
    """План поискового запроса с теми же настройками, что в приложении, текстом JSON."""
    sessions = async_sessionmaker(big.engine, expire_on_commit=False)
    statement = search_statement(uuid.uuid4(), query, limit=21, offset=0)
    async with sessions() as session:
        for name, value in SEARCH_SETTINGS.items():
            await session.execute(
                text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
            )
        sql = str(
            statement.compile(dialect=big.engine.dialect, compile_kwargs={"literal_binds": True})
        )
        connection = await session.connection()
        rows = (await connection.exec_driver_sql("EXPLAIN (FORMAT JSON) " + sql)).all()
    plan: Any = rows[0][0]
    return json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize(
    ("query", "index"),
    [
        ("иван петров", "ix_profiles_display_name_trgm"),
        ("петров иван", "ix_profiles_display_name_trgm"),
        ("анна иванова", "ix_profiles_display_name_trgm"),
        ("ёлкин", "ix_profiles_display_name_trgm"),
        ("john smith", "ix_profiles_display_name_trgm"),
        # Ник, на который не похож никто: условия ника избирательны, и планировщик берёт оба индекса.
        ("zqzq", "ix_users_username_prefix"),
        ("zqzq", "ix_users_username_trgm"),
    ],
)
async def test_the_planner_reads_names_and_logins_through_the_indexes(
    big: Big, query: str, index: str
) -> None:
    assert index in await plan_of(big, query)
