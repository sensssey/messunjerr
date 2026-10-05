"""S3-07: `make seed`: учебные аккаунты с профилями, повтор безопасен, в prod и stage запрещён."""

import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.cli import build_parser, main
from messunjerr.seeding import BATCH_SIZE, SEED_PASSWORD, email_for, seed_database, username_for
from messunjerr.settings import Settings

from .helpers import bearer, fetch_all, fetch_one


async def test_seed_creates_active_accounts_each_with_a_profile_and_privacy(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    result = await seed_database(test_settings, users=12)

    assert (result.created, result.existing) == (12, 0)
    users = await fetch_all(
        admin_engine,
        "SELECT u.username::text AS username, u.email::text AS email, u.status, "
        "u.email_verified_at IS NOT NULL AS verified, u.terms_version, "
        "p.display_name, p.is_private, p.birth_date, p.birth_date_visibility "
        "FROM identity.users u JOIN profile.profiles p ON p.user_id = u.id "
        "JOIN profile.privacy_settings s ON s.user_id = u.id ORDER BY u.username",
    )
    assert [u["username"] for u in users] == [username_for(n) for n in range(1, 13)]
    assert all(u["status"] == "active" and u["verified"] for u in users)
    assert users[0]["email"] == email_for(1) == "seed_0001@example.com"
    assert {u["terms_version"] for u in users} == {test_settings.legal_terms_version}
    assert all(1 <= len(u["display_name"]) <= 50 for u in users)


async def test_seed_profiles_are_varied_enough_to_exercise_the_policies(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_database(test_settings, users=60)

    private = await fetch_one(
        admin_engine, "SELECT count(*) AS n FROM profile.profiles WHERE is_private"
    )
    assert private["n"] == 12  # каждый пятый
    visibilities = await fetch_all(
        admin_engine, "SELECT DISTINCT birth_date_visibility AS v FROM profile.profiles"
    )
    assert {row["v"] for row in visibilities} == {"hidden", "day_month", "full"}
    without_date = await fetch_one(
        admin_engine, "SELECT count(*) AS n FROM profile.profiles WHERE birth_date IS NULL"
    )
    assert 0 < without_date["n"] < 60
    assert (
        await fetch_one(
            admin_engine, "SELECT count(*) AS n FROM profile.profiles WHERE links <> '[]'::jsonb"
        )
    )["n"] > 0
    open_lists = await fetch_one(
        admin_engine,
        "SELECT count(*) AS n FROM profile.privacy_settings WHERE friends_list_visibility = 'everyone'",
    )
    assert 0 < open_lists["n"] < 60


async def test_every_seeded_person_is_an_adult(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    await seed_database(test_settings, users=120)

    youngest = await fetch_one(
        admin_engine,
        "SELECT min(date_part('year', age(birth_date))) AS years FROM profile.profiles "
        "WHERE birth_date IS NOT NULL",
    )
    assert youngest["years"] >= test_settings.min_age


async def test_seeding_twice_changes_nothing_and_a_larger_run_adds_the_rest(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    first = await seed_database(test_settings, users=8)
    again = await seed_database(test_settings, users=8)
    more = await seed_database(test_settings, users=11)

    assert (first.created, again.created, again.existing) == (8, 0, 8)
    assert (more.created, more.existing) == (3, 8)
    count = await fetch_one(admin_engine, "SELECT count(*) AS n FROM identity.users")
    assert count["n"] == 11
    profiles = await fetch_one(admin_engine, "SELECT count(*) AS n FROM profile.profiles")
    assert profiles["n"] == 11


async def test_a_large_seed_is_inserted_in_batches(
    test_settings: Settings, admin_engine: AsyncEngine
) -> None:
    total = BATCH_SIZE + 7

    result = await seed_database(test_settings, users=total)

    assert result.created == total
    count = await fetch_one(admin_engine, "SELECT count(*) AS n FROM profile.privacy_settings")
    assert count["n"] == total


async def test_seeded_people_sign_in_and_see_each_other_by_the_rules(
    test_settings: Settings, client: httpx.AsyncClient
) -> None:
    await seed_database(test_settings, users=10)

    login = await client.post(
        "/api/v1/auth/login", json={"login": username_for(1), "password": SEED_PASSWORD}
    )
    by_email = await client.post(
        "/api/v1/auth/login", json={"login": email_for(2), "password": SEED_PASSWORD}
    )

    assert login.status_code == 200, login.text
    assert by_email.status_code == 200
    me = login.json()["user"]
    assert me["status"] == "active"
    assert me["profile"]["display_name"]
    headers = bearer(login.json()["access_token"])
    closed = await client.get(
        f"/api/v1/users/{username_for(5)}", headers=headers
    )  # каждый пятый закрыт
    opened = await client.get(f"/api/v1/users/{username_for(4)}", headers=headers)
    assert closed.json()["is_private"] is True
    assert closed.json()["links"] == []
    assert closed.json()["city"] is None
    assert opened.json()["is_private"] is False


async def test_a_custom_password_is_used_for_everyone(
    test_settings: Settings, client: httpx.AsyncClient
) -> None:
    await seed_database(test_settings, users=2, password="another seed password 77")

    ok = await client.post(
        "/api/v1/auth/login",
        json={"login": username_for(2), "password": "another seed password 77"},
    )
    default = await client.post(
        "/api/v1/auth/login", json={"login": username_for(2), "password": SEED_PASSWORD}
    )

    assert ok.status_code == 200
    assert default.status_code == 401


@pytest.mark.parametrize("env", ["prod", "stage"])
async def test_seeding_is_refused_outside_development(test_settings: Settings, env: str) -> None:
    settings = test_settings.model_copy(update={"app_env": env})

    with pytest.raises(RuntimeError, match="seed запрещён"):
        await seed_database(settings, users=3)


@pytest.mark.parametrize("users", [0, -5])
async def test_the_number_of_accounts_must_be_positive(test_settings: Settings, users: int) -> None:
    with pytest.raises(ValueError, match="положительным"):
        await seed_database(test_settings, users=users)


# ----------------------------------------------------------------------------- командная строка
def test_the_cli_knows_the_seed_command() -> None:
    args = build_parser().parse_args(["seed", "--users", "5", "--password", "x"])
    assert (args.command, args.users, args.password) == ("seed", 5, "x")
    defaults = build_parser().parse_args(["seed"])
    assert (defaults.users, defaults.password) == (30, None)


async def test_the_seed_command_reports_what_it_did(
    test_settings: Settings,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("messunjerr.cli.get_settings", lambda: test_settings)

    code = await asyncio.to_thread(main, ["seed", "--users", "4"])

    out = capsys.readouterr().out
    assert code == 0
    assert "создано аккаунтов 4" in out
    assert "seed_0001" in out
    assert (await fetch_one(admin_engine, "SELECT count(*) AS n FROM identity.users"))["n"] == 4


async def test_the_seed_command_fails_cleanly_in_prod(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prod = test_settings.model_copy(update={"app_env": "prod"})
    monkeypatch.setattr("messunjerr.cli.get_settings", lambda: prod)

    code = await asyncio.to_thread(main, ["seed"])

    captured = capsys.readouterr()
    assert code == 1
    assert "seed запрещён" in captured.err
    assert captured.out == ""
