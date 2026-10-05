"""S3-03: `PATCH /me/username`: пауза между сменами, резерв прежнего ника, гонки, очистка резервов."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.identity.commands.housekeeping import purge_username_reservations
from messunjerr.settings import Settings

from .helpers import (
    PASSWORD,
    SignedInUser,
    client_with,
    execute,
    fetch_all,
    fetch_one,
    register,
    verified_user,
)

USERNAME = "/api/v1/me/username"
AVAILABLE = "/api/v1/auth/username-available"


async def rename(client: httpx.AsyncClient, user: SignedInUser, name: str) -> httpx.Response:
    return await client.patch(USERNAME, json={"username": name}, headers=user.headers)


async def available(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(AVAILABLE, params={"username": name})).json()
    return body


async def forget_the_last_change(engine: AsyncEngine, *, days_ago: float) -> None:
    await execute(
        engine,
        "UPDATE identity.users SET username_changed_at = now() - make_interval(secs => :s)",
        s=days_ago * 86400,
    )


async def reservations(engine: AsyncEngine) -> list[str]:
    rows = await fetch_all(
        engine, "SELECT username::text AS u FROM identity.username_reservations ORDER BY 1"
    )
    return [row["u"] for row in rows]


def fresh_name() -> str:
    return f"name_{uuid.uuid4().hex[:10]}"


# ----------------------------------------------------------------------------- первая смена
async def test_the_first_change_is_free_and_takes_effect_everywhere(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    old, new = user.credentials["username"], fresh_name()

    response = await rename(client, user, new.upper())

    assert response.status_code == 200, response.text
    assert response.json() == {"username": new}  # ник хранится в нижнем регистре
    assert response.headers["cache-control"] == "no-store"
    me = (await client.get("/api/v1/me", headers=user.headers)).json()
    assert me["username"] == new
    assert (await client.get(f"/api/v1/users/{new}", headers=user.headers)).status_code == 200
    assert (await client.get(f"/api/v1/users/{old}", headers=user.headers)).status_code == 404
    row = await fetch_one(admin_engine, "SELECT username_changed_at FROM identity.users")
    assert row["username_changed_at"] is not None
    assert await reservations(admin_engine) == [old]


async def test_login_works_with_the_new_name_and_not_with_the_old_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    old, new = user.credentials["username"], fresh_name()
    await rename(client, user, new)

    with_new = await client.post("/api/v1/auth/login", json={"login": new, "password": PASSWORD})
    with_old = await client.post("/api/v1/auth/login", json={"login": old, "password": PASSWORD})

    assert with_new.status_code == 200
    assert with_new.json()["user"]["username"] == new
    assert (with_old.status_code, with_old.json()["code"]) == (401, "invalid_credentials")


async def test_the_rest_of_the_account_is_not_touched_by_a_rename(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs, display_name="Иван")
    before = (await client.get("/api/v1/me", headers=user.headers)).json()

    await rename(client, user, fresh_name())

    after = (await client.get("/api/v1/me", headers=user.headers)).json()
    assert {k: v for k, v in after.items() if k != "username"} == {
        k: v for k, v in before.items() if k != "username"
    }


async def test_the_change_is_audited_without_putting_names_into_the_journal(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    old, new = user.credentials["username"], fresh_name()

    await rename(client, user, new)

    rows = await fetch_all(
        admin_engine, "SELECT * FROM platform.audit_log WHERE action = 'username.changed'"
    )
    (entry,) = rows
    assert str(entry["actor_id"]) == str(entry["target_id"]) == user.user_id
    assert entry["target_type"] == "user"
    assert entry["data"] == {}
    assert old not in str(entry)
    assert new not in str(entry)


# ----------------------------------------------------------------------------- пауза
async def test_the_second_change_waits_thirty_days(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await rename(client, user, fresh_name())

    blocked = await rename(client, user, fresh_name())

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "username_change_cooldown"
    assert blocked.json()["retry_after_days"] == 30
    assert blocked.headers["content-type"] == "application/problem+json"
    assert len(await reservations(admin_engine)) == 1  # неудачная попытка ничего не резервирует


@pytest.mark.parametrize(
    ("days_ago", "retry_after_days"),
    [(0.5, 30), (10, 20), (29, 1), (29.99, 1)],
)
async def test_the_wait_is_reported_in_whole_days_rounded_up(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    days_ago: float,
    retry_after_days: int,
) -> None:
    user = await verified_user(client, jobs)
    await rename(client, user, fresh_name())
    await forget_the_last_change(admin_engine, days_ago=days_ago)

    blocked = await rename(client, user, fresh_name())

    assert blocked.status_code == 409
    assert blocked.json()["retry_after_days"] == retry_after_days


async def test_after_thirty_days_the_name_can_be_changed_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    first, second = fresh_name(), fresh_name()
    await rename(client, user, first)
    await forget_the_last_change(admin_engine, days_ago=30.001)

    response = await rename(client, user, second)

    assert response.status_code == 200
    assert response.json() == {"username": second}
    assert user.credentials["username"] in await reservations(admin_engine)
    assert first in await reservations(admin_engine)


async def test_the_pause_is_a_setting(
    test_settings: Settings, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    async with client_with(test_settings, jobs, username_change_cooldown_days=7) as short:
        user = await verified_user(short, jobs)
        await rename(short, user, fresh_name())
        await forget_the_last_change(admin_engine, days_ago=3)

        blocked = await rename(short, user, fresh_name())
        await forget_the_last_change(admin_engine, days_ago=7.01)
        allowed = await rename(short, user, fresh_name())

        assert (blocked.status_code, blocked.json()["retry_after_days"]) == (409, 4)
        assert allowed.status_code == 200


async def test_without_a_pause_there_is_no_reservation_either(
    test_settings: Settings, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    async with client_with(test_settings, jobs, username_change_cooldown_days=0) as free:
        user = await verified_user(free, jobs)

        first = await rename(free, user, fresh_name())
        second = await rename(free, user, fresh_name())

        assert (first.status_code, second.status_code) == (200, 200)
        assert await reservations(admin_engine) == []


# ----------------------------------------------------------------------------- прежний ник занят
async def test_the_old_name_stays_taken_after_a_rename(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner = await verified_user(client, jobs)
    other = await verified_user(client, jobs)
    old = owner.credentials["username"]
    await rename(client, owner, fresh_name())

    assert await available(client, old) == {"available": False, "reason": "taken"}
    registered, _ = await register(client, username=old)
    assert (registered.status_code, registered.json()["code"]) == (409, "username_taken")
    taken = await rename(client, other, old)
    assert (taken.status_code, taken.json()["code"]) == (409, "username_taken")
    assert (await rename(client, other, old.upper())).status_code == 409


async def test_the_old_name_is_free_again_when_the_reservation_runs_out(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    owner = await verified_user(client, jobs)
    other = await verified_user(client, jobs)
    old = owner.credentials["username"]
    await rename(client, owner, fresh_name())

    await execute(
        admin_engine,
        "UPDATE identity.username_reservations SET reserved_until = now() - interval '1 second'",
    )

    assert await available(client, old) == {"available": True, "reason": None}
    assert (await rename(client, other, old)).status_code == 200
    # Просроченную запись очистка ещё не удалила, но имя уже у другого человека.
    assert (await client.get(f"/api/v1/users/{old}", headers=owner.headers)).status_code == 200


async def test_a_new_owner_of_a_released_name_can_release_it_again(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    """Запись о резерве с истёкшим сроком не мешает поставить новый резерв на то же имя."""
    first = await verified_user(client, jobs)
    second = await verified_user(client, jobs)
    name = first.credentials["username"]
    await rename(client, first, fresh_name())
    await execute(
        admin_engine,
        "UPDATE identity.username_reservations SET reserved_until = now() - interval '1 second'",
    )
    assert (await rename(client, second, name)).status_code == 200
    await forget_the_last_change(admin_engine, days_ago=31)

    response = await rename(client, second, fresh_name())

    assert response.status_code == 200
    row = await fetch_one(
        admin_engine,
        "SELECT user_id, reserved_until > now() AS active FROM identity.username_reservations "
        "WHERE username = :u",
        u=name,
    )
    assert str(row["user_id"]) == second.user_id
    assert row["active"] is True


async def test_the_owner_may_take_back_their_own_old_name(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    old = user.credentials["username"]
    await rename(client, user, fresh_name())
    await forget_the_last_change(admin_engine, days_ago=31)  # пауза прошла, резерв ещё действует

    response = await rename(client, user, old)

    assert response.status_code == 200
    assert response.json() == {"username": old}


async def test_a_chain_of_renames_reserves_every_name_it_leaves_behind(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    a = user.credentials["username"]
    b, c = fresh_name(), fresh_name()

    await rename(client, user, b)
    await forget_the_last_change(admin_engine, days_ago=31)
    await rename(client, user, c)

    assert sorted(await reservations(admin_engine)) == sorted([a, b])


async def test_a_pending_account_may_not_take_a_reserved_name_by_registering(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner = await verified_user(client, jobs)
    old = owner.credentials["username"]
    await rename(client, owner, fresh_name())

    # Повторная регистрация неподтверждённого адреса с занятым резервом ником: тоже 409.
    first, body = await register(client)
    assert first.status_code == 201
    again = await client.post(
        "/api/v1/auth/register", json={**body, "username": old, "password": PASSWORD}
    )

    assert again.status_code == 409
    assert again.json()["code"] == "username_taken"


# ----------------------------------------------------------------------------- «тот же ник» и чужие ники
async def test_the_same_name_is_not_a_change(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    name = user.credentials["username"]

    same = await rename(client, user, name)
    shouting = await rename(client, user, name.upper())

    assert (same.status_code, shouting.status_code) == (200, 200)
    assert same.json() == shouting.json() == {"username": name}
    row = await fetch_one(admin_engine, "SELECT username_changed_at FROM identity.users")
    assert row["username_changed_at"] is None  # пауза не запущена
    assert await reservations(admin_engine) == []
    assert (
        await fetch_all(
            admin_engine, "SELECT 1 FROM platform.audit_log WHERE action = 'username.changed'"
        )
        == []
    )


@pytest.mark.parametrize("variant", ["same", "upper"])
async def test_somebody_elses_name_is_taken(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, variant: str
) -> None:
    owner = await verified_user(client, jobs)
    user = await verified_user(client, jobs)
    wanted = owner.credentials["username"]

    response = await rename(client, user, wanted if variant == "same" else wanted.upper())

    assert response.status_code == 409
    assert response.json()["code"] == "username_taken"
    # Неудачная попытка не запускает паузу: можно сразу выбрать другое имя.
    assert (await rename(client, user, fresh_name())).status_code == 200


async def test_the_name_of_a_pending_account_is_taken_too(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    _, pending = await register(client)

    response = await rename(client, user, pending["username"])

    assert response.status_code == 409


# ----------------------------------------------------------------------------- проверки формата
@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("admin", "username_reserved"),
        ("Support", "username_reserved"),
        ("api", "username_reserved"),
        ("ab", "string_too_short"),
        ("x" * 31, "string_too_long"),
        ("bad name", "invalid_format"),
        ("иван", "invalid_format"),
        ("a-b-c", "invalid_format"),
        ("", "string_too_short"),
    ],
)
async def test_invalid_names_give_422_and_do_not_touch_the_account(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    name: str,
    code: str,
) -> None:
    user = await verified_user(client, jobs)

    response = await rename(client, user, name)

    assert response.status_code == 422
    assert [(e["pointer"], e["code"]) for e in response.json()["errors"]] == [
        ("/body/username", code)
    ]
    row = await fetch_one(
        admin_engine, "SELECT username::text AS u, username_changed_at FROM identity.users"
    )
    assert row["u"] == user.credentials["username"]
    assert row["username_changed_at"] is None


async def test_the_username_field_is_required_and_nothing_else_is_accepted(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    empty = await client.patch(USERNAME, json={}, headers=user.headers)
    extra = await client.patch(
        USERNAME, json={"username": fresh_name(), "email": "x@example.com"}, headers=user.headers
    )

    assert [(e["pointer"], e["code"]) for e in empty.json()["errors"]] == [
        ("/body/username", "required")
    ]
    assert [(e["pointer"], e["code"]) for e in extra.json()["errors"]] == [
        ("/body/email", "unknown_field")
    ]


# ----------------------------------------------------------------------------- гонки
async def test_two_people_racing_for_one_name_leave_one_winner(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice = await verified_user(client, jobs)
    bob = await verified_user(client, jobs)
    prize = fresh_name()

    results = await asyncio.gather(rename(client, alice, prize), rename(client, bob, prize))

    assert sorted(r.status_code for r in results) == [200, 409]


async def test_parallel_changes_by_one_person_count_as_one(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)

    results = await asyncio.gather(
        *(rename(client, user, fresh_name()) for _ in range(4)), return_exceptions=False
    )

    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]
    assert len(await reservations(admin_engine)) == 1


async def test_a_registration_cannot_grab_a_name_released_a_moment_ago(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    owner = await verified_user(client, jobs)
    old = owner.credentials["username"]

    results = await asyncio.gather(
        rename(client, owner, fresh_name()), register(client, username=old)
    )

    _, (registration, _) = results
    # Либо регистрация успела раньше (тогда смена ника у владельца всё равно удалась, но ник занят
    # самим владельцем до коммита), либо резерв уже стоит: в обоих случаях второй владелец
    # получить освободившееся имя не может.
    assert registration.status_code == 409


# ----------------------------------------------------------------------------- очистка
async def test_the_cleanup_removes_only_expired_reservations(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(3):
        await rename(client, await verified_user(client, jobs), fresh_name())
    await execute(
        admin_engine,
        "UPDATE identity.username_reservations SET reserved_until = now() - interval '1 day' "
        "WHERE username = (SELECT username FROM identity.username_reservations ORDER BY username LIMIT 1)",
    )

    removed = await purge_username_reservations(sessionmaker)

    assert removed == 1
    assert len(await reservations(admin_engine)) == 2
    assert await purge_username_reservations(sessionmaker) == 0


async def test_the_cleanup_runs_in_batches(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    admin_engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(5):
        await rename(client, await verified_user(client, jobs), fresh_name())
    await execute(
        admin_engine,
        "UPDATE identity.username_reservations SET reserved_until = :moment",
        moment=utcnow() - timedelta(days=1),
    )

    assert await purge_username_reservations(sessionmaker, batch_size=2) == 5
    assert await reservations(admin_engine) == []


async def test_a_reservation_goes_away_with_its_account(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    await rename(client, user, fresh_name())
    assert len(await reservations(admin_engine)) == 1

    await execute(admin_engine, "DELETE FROM identity.users")

    assert await reservations(admin_engine) == []


# ----------------------------------------------------------------------------- доступ
async def test_the_change_requires_a_token(client: httpx.AsyncClient) -> None:
    response = await client.patch(USERNAME, json={"username": fresh_name()})

    assert response.status_code == 401
    assert response.json()["code"] == "token_missing"


async def test_the_endpoint_is_documented(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    operation = schema["paths"]["/api/v1/me/username"]["patch"]
    assert {"200", "401", "403", "409", "422", "429"} <= set(operation["responses"])
