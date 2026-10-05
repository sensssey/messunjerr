"""S3-02: `PATCH /me/profile` (merge-patch, проверки полей, возраст, аватар) и разделы профиля в `GET /me`."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.settings import Settings

from .helpers import ME, SignedInUser, client_with, fetch_one, limited_client, verified_user

PROFILE = "/api/v1/me/profile"

PROFILE_KEYS = {
    "display_name",
    "avatar",
    "bio",
    "links",
    "birth_date",
    "birth_date_visibility",
    "city",
    "language",
    "timezone",
    "is_private",
    "hidden_fields",
}


async def patch(client: httpx.AsyncClient, user: SignedInUser, **body: Any) -> httpx.Response:
    return await client.patch(PROFILE, json=body, headers=user.headers)


async def my_profile(client: httpx.AsyncClient, user: SignedInUser) -> dict[str, Any]:
    profile: dict[str, Any] = (await client.get(ME, headers=user.headers)).json()["profile"]
    return profile


def years_ago(years: int, *, extra_days: int = 0) -> date:
    """Дата `years` лет назад от сегодняшней (UTC) плюс `extra_days` дней вперёд."""
    today = datetime.now(UTC).date()
    try:
        base = today.replace(year=today.year - years)
    except ValueError:  # 29 февраля
        base = today.replace(year=today.year - years, day=28)
    return base + timedelta(days=extra_days)


def errors_of(response: httpx.Response) -> list[tuple[str, str]]:
    assert response.status_code == 422, response.text
    return [(e["pointer"], e["code"]) for e in response.json()["errors"]]


# ----------------------------------------------------------------------------- успешные правки
async def test_a_user_edits_the_profile_and_gets_it_back_in_full(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(
        client,
        user,
        display_name="Иван Петров",
        bio="Люблю горы",
        links=[{"title": "Блог", "url": "https://example.com"}],
        birth_date="1990-05-12",
        birth_date_visibility="day_month",
        city="Казань",
        language="ru",
        timezone="Europe/Moscow",
        is_private=True,
    )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "display_name": "Иван Петров",
        "avatar": None,
        "bio": "Люблю горы",
        "links": [{"title": "Блог", "url": "https://example.com"}],
        "birth_date": "1990-05-12",
        "birth_date_visibility": "day_month",
        "city": "Казань",
        "language": "ru",
        "timezone": "Europe/Moscow",
        "is_private": True,
        "hidden_fields": [],
    }
    assert await my_profile(client, user) == response.json()


async def test_untouched_fields_keep_their_values(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await patch(client, user, bio="Первое", city="Казань", is_private=True)

    response = await patch(client, user, bio="Второе")

    profile = response.json()
    assert (profile["bio"], profile["city"], profile["is_private"]) == ("Второе", "Казань", True)
    assert profile["display_name"] == user.credentials["username"]


async def test_null_clears_the_fields_that_allow_it(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await patch(
        client,
        user,
        bio="x",
        city="y",
        birth_date="1990-05-12",
        language="ru",
        timezone="Europe/Moscow",
        links=[{"title": "t", "url": "https://example.com"}],
    )

    cleared = await patch(
        client,
        user,
        bio=None,
        city=None,
        birth_date=None,
        language=None,
        timezone=None,
        links=None,
        avatar_asset_id=None,
    )

    profile = cleared.json()
    assert cleared.status_code == 200
    assert (profile["bio"], profile["city"], profile["birth_date"]) == (None, None, None)
    assert (profile["language"], profile["timezone"], profile["links"]) == (None, None, [])


@pytest.mark.parametrize("field", ["bio", "city"])
async def test_an_empty_string_clears_free_text_fields(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine, field: str
) -> None:
    user = await verified_user(client, jobs)
    await patch(client, user, **{field: "что-то"})

    response = await patch(client, user, **{field: "   "})

    assert response.json()[field] is None
    row = await fetch_one(admin_engine, f"SELECT {field} FROM profile.profiles")
    assert row[field] is None  # в БД NULL, а не пустая строка


async def test_an_empty_body_is_accepted_and_changes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    before = await my_profile(client, user)

    response = await patch(client, user)

    assert response.status_code == 200
    assert response.json() == before


async def test_links_are_replaced_as_a_whole_and_keep_their_order(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    first = [{"title": f"Ссылка {n}", "url": f"https://example.com/{n}"} for n in range(5)]
    assert (await patch(client, user, links=first)).json()["links"] == first

    second = [{"title": "Одна", "url": "http://example.org"}]

    assert (await patch(client, user, links=second)).json()["links"] == second
    assert (await patch(client, user, links=[])).json()["links"] == []


async def test_strings_are_trimmed_and_normalized_before_they_are_stored(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, display_name="  Zoé  ", city=" Казань\n")

    assert response.json()["display_name"] == "Zoé"
    assert response.json()["city"] == "Казань"


async def test_language_is_stored_in_the_canonical_spelling(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    assert (await patch(client, user, language="EN-us")).json()["language"] == "en-US"


async def test_the_privacy_flag_can_be_switched_back_and_forth(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    assert (await patch(client, user, is_private=True)).json()["is_private"] is True
    assert (await patch(client, user, is_private=False)).json()["is_private"] is False


async def test_only_the_owners_own_profile_is_changed(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    alice = await verified_user(client, jobs)
    bob = await verified_user(client, jobs)

    await patch(client, alice, bio="про Алису", city="Москва")

    assert (await my_profile(client, bob))["bio"] is None
    assert (await my_profile(client, bob))["city"] is None


async def test_the_edit_moves_updated_at_forward(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, admin_engine: AsyncEngine
) -> None:
    user = await verified_user(client, jobs)
    before = await fetch_one(admin_engine, "SELECT created_at, updated_at FROM profile.profiles")

    await patch(client, user, bio="новое")

    after = await fetch_one(admin_engine, "SELECT created_at, updated_at FROM profile.profiles")
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] > before["updated_at"]


# ----------------------------------------------------------------------------- ошибки полей
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"display_name": ""}, [("/body/display_name", "string_too_short")]),
        ({"display_name": "я" * 51}, [("/body/display_name", "string_too_long")]),
        ({"display_name": None}, [("/body/display_name", "invalid_format")]),
        ({"bio": "я" * 501}, [("/body/bio", "string_too_long")]),
        ({"city": "я" * 101}, [("/body/city", "string_too_long")]),
        (
            {"links": [{"title": "t", "url": "https://e.com"}] * 6},
            [("/body/links", "too_many_items")],
        ),
        (
            {"links": [{"title": "t", "url": "javascript:alert(1)"}]},
            [("/body/links/0/url", "invalid_format")],
        ),
        (
            {"links": [{"title": "", "url": "https://e.com"}]},
            [("/body/links/0/title", "string_too_short")],
        ),
        ({"links": [{"url": "https://e.com"}]}, [("/body/links/0/title", "required")]),
        ({"birth_date": "12.05.1990"}, [("/body/birth_date", "invalid_format")]),
        ({"birth_date_visibility": "everyone"}, [("/body/birth_date_visibility", "invalid_enum")]),
        ({"birth_date_visibility": None}, [("/body/birth_date_visibility", "invalid_format")]),
        ({"is_private": "yes"}, [("/body/is_private", "invalid_format")]),
        ({"is_private": None}, [("/body/is_private", "invalid_format")]),
        ({"language": "russian"}, [("/body/language", "invalid_format")]),
        ({"timezone": "Moscow"}, [("/body/timezone", "invalid_format")]),
        ({"avatar_asset_id": "not-a-uuid"}, [("/body/avatar_asset_id", "invalid_format")]),
        ({"username": "other"}, [("/body/username", "unknown_field")]),
        ({"role": "admin"}, [("/body/role", "unknown_field")]),
    ],
)
async def test_invalid_fields_give_422_with_pointers_and_catalog_codes(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    body: dict[str, Any],
    expected: list[tuple[str, str]],
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, **body)

    assert errors_of(response) == expected
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "validation_error"


async def test_error_responses_do_not_echo_the_input(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, bio="СЕКРЕТНЫЙ ТЕКСТ" * 100, city="Тайный город" * 20)

    assert response.status_code == 422
    assert "СЕКРЕТНЫЙ" not in response.text
    assert "Тайный" not in response.text


async def test_a_rejected_request_changes_nothing(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    before = await my_profile(client, user)

    response = await patch(client, user, display_name="Допустимое", birth_date="1990-99-99")

    assert response.status_code == 422
    assert await my_profile(client, user) == before


async def test_every_problem_is_reported_in_one_response(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, display_name="", language="??", bio="я" * 501)

    assert {pointer for pointer, _ in errors_of(response)} == {
        "/body/display_name",
        "/body/language",
        "/body/bio",
    }


async def test_a_body_that_is_not_json_is_an_invalid_request(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await client.patch(
        PROFILE, content=b"{broken", headers={**user.headers, "content-type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


# ----------------------------------------------------------------------------- возраст
async def test_birth_date_must_give_the_minimum_age(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    exactly_18_today = years_ago(18)
    one_day_short = years_ago(18, extra_days=1)

    ok = await patch(client, user, birth_date=exactly_18_today.isoformat())
    assert ok.status_code == 200
    assert ok.json()["birth_date"] == exactly_18_today.isoformat()
    underage = await patch(client, user, birth_date=one_day_short.isoformat())
    assert errors_of(underage) == [("/body/birth_date", "underage")]
    assert underage.json()["errors"][0]["meta"] == {"min_age": 18}
    assert (await my_profile(client, user))["birth_date"] == exactly_18_today.isoformat()


@pytest.mark.parametrize(
    "birth_date",
    [
        (datetime.now(UTC).date().replace(year=datetime.now(UTC).year + 1)).isoformat(),
        "1800-01-01",
        "0001-01-01",
    ],
)
async def test_impossible_birth_dates_are_out_of_range(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, birth_date: str
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(client, user, birth_date=birth_date)

    assert errors_of(response) == [("/body/birth_date", "out_of_range")]


async def test_the_minimum_age_is_a_setting(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with client_with(test_settings, jobs, min_age=21) as strict:
        user = await verified_user(strict, jobs)

        too_young = await patch(strict, user, birth_date=years_ago(20).isoformat())
        old_enough = await patch(strict, user, birth_date=years_ago(21).isoformat())

        assert errors_of(too_young) == [("/body/birth_date", "underage")]
        assert too_young.json()["errors"][0]["meta"] == {"min_age": 21}
        assert old_enough.status_code == 200


async def test_clearing_the_birth_date_needs_no_age_check(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)
    await patch(client, user, birth_date=years_ago(30).isoformat())

    assert (await patch(client, user, birth_date=None)).json()["birth_date"] is None


# ----------------------------------------------------------------------------- аватар
async def test_an_avatar_asset_is_not_found_until_media_exists(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    """План S3-02: пока нет загрузок (S5–S6), любой `avatar_asset_id` даёт `asset_not_found`."""
    user = await verified_user(client, jobs)

    response = await patch(client, user, avatar_asset_id=str(uuid.uuid4()))

    assert errors_of(response) == [("/body/avatar_asset_id", "asset_not_found")]
    assert (await my_profile(client, user))["avatar"] is None


async def test_avatar_and_age_problems_come_together(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue
) -> None:
    user = await verified_user(client, jobs)

    response = await patch(
        client,
        user,
        birth_date=years_ago(10).isoformat(),
        avatar_asset_id=str(uuid.uuid4()),
    )

    assert sorted(errors_of(response)) == [
        ("/body/avatar_asset_id", "asset_not_found"),
        ("/body/birth_date", "underage"),
    ]


# ----------------------------------------------------------------------------- доступ и лимиты
async def test_the_edit_requires_a_token(client: httpx.AsyncClient) -> None:
    response = await client.patch(PROFILE, json={"bio": "x"})

    assert response.status_code == 401
    assert response.json()["code"] == "token_missing"


async def test_a_revoked_session_cannot_edit(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, redis_client: Redis
) -> None:
    user = await verified_user(client, jobs)
    await redis_client.set(f"sess:revoked:{user.session_id}", "1", ex=600)

    response = await patch(client, user, bio="x")

    assert response.status_code == 401
    assert response.json()["code"] == "session_revoked"


async def test_edits_share_the_write_limit(test_settings: Settings, jobs: InMemoryJobQueue) -> None:
    async with limited_client(test_settings, jobs, api_write=3) as (_, http):
        user = await verified_user(http, jobs)

        statuses = [(await patch(http, user, bio=f"v{n}")).status_code for n in range(5)]

        assert statuses == [200, 200, 200, 429, 429]
        limited = await patch(http, user, bio="x")
        assert limited.json()["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.headers["ratelimit-limit"] == "3"


async def test_the_endpoint_is_documented(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    operation = schema["paths"]["/api/v1/me/profile"]["patch"]
    assert {"200", "401", "403", "422", "429"} <= set(operation["responses"])
    request = schema["components"]["schemas"]["UpdateProfileRequest"]
    assert request["examples"][0]["display_name"] == "Иван"
    assert set(request["properties"]) == set(PROFILE_KEYS) - {"avatar", "hidden_fields"} | {
        "avatar_asset_id"
    }
