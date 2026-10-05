"""Лимиты запросов на уровне API (S2-05): перебор паролей, регистрация, письма, ники, refresh, сбой Redis."""

import asyncio
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.main import create_app
from messunjerr.settings import Settings

from .helpers import (
    LOGIN,
    ME,
    NEW_PASSWORD,
    PASSWORD,
    REFRESH,
    SignedInUser,
    cookie_header,
    do_refresh,
    limited_client,
    new_credentials,
    refresh_headers,
    verified_user,
)

REGISTER = "/api/v1/auth/register"
RESEND = "/api/v1/auth/resend-verification"
FORGOT = "/api/v1/auth/password/forgot"
CHANGE = "/api/v1/auth/password/change"
USERNAME = "/api/v1/auth/username-available"


async def wrong_login(http: httpx.AsyncClient, login: str) -> httpx.Response:
    return await http.post(LOGIN, json={"login": login, "password": "wrong password!"})


def shape(response: httpx.Response) -> dict[str, Any]:
    """Тело ошибки без полей, которые у каждого запроса свои."""
    skip = {"request_id", "instance", "retry_after"}
    return {k: v for k, v in response.json().items() if k not in skip}


# ----------------------------------------------------------------------------- вход: адрес
async def test_the_21st_login_attempt_from_one_address_is_refused(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    """Демо S2: 21-я попытка входа с одного IP за 10 минут даёт 429 (лимиты по умолчанию)."""
    async with limited_client(test_settings, jobs) as (_, http):
        statuses = [
            (await wrong_login(http, f"nobody-{n}@example.com")).status_code for n in range(21)
        ]

    assert statuses[:20] == [401] * 20
    assert statuses[20] == 429


async def test_every_response_shows_the_remaining_budget_and_429_says_when_to_retry(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_login_ip=3) as (_, http):
        answers = [await wrong_login(http, f"someone-{n}@example.com") for n in range(3)]
        refused = await wrong_login(http, "someone-else@example.com")

    for number, answer in enumerate(answers):
        assert answer.status_code == 401  # ответ-ошибка тоже несёт заголовки лимита
        assert answer.headers["ratelimit-limit"] == "3"
        assert answer.headers["ratelimit-remaining"] == str(2 - number)
        assert int(answer.headers["ratelimit-reset"]) > 0
    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    body = refused.json()
    assert body["code"] == "rate_limited"
    assert body["status"] == 429
    assert body["retry_after"] >= 1
    assert refused.headers["retry-after"] == str(body["retry_after"])
    assert refused.headers["ratelimit-remaining"] == "0"
    assert refused.headers["cache-control"] == "no-store"


async def test_successful_responses_carry_the_budget_too(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_login_ip=5) as (_, http):
        user = await verified_user(http, jobs)
        response = await http.post(
            LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
        )

    assert response.status_code == 200
    assert response.headers["ratelimit-limit"] == "5"
    assert response.headers["ratelimit-remaining"] == "4"


# ----------------------------------------------------------------------------- вход: аккаунт
async def test_failed_attempts_per_account_are_limited_even_for_the_right_password(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_login_account=3, auth_login_ip=100) as (
        _,
        http,
    ):
        victim = await verified_user(http, jobs)
        other = await verified_user(http, jobs)
        email = victim.credentials["email"]

        guesses = [(await wrong_login(http, email)).status_code for _ in range(3)]
        fourth = await wrong_login(http, email)
        right_but_late = await http.post(LOGIN, json={"login": email, "password": PASSWORD})
        neighbour = await http.post(
            LOGIN, json={"login": other.credentials["email"], "password": PASSWORD}
        )

    assert guesses == [401, 401, 401]
    assert fourth.status_code == 429
    assert right_but_late.status_code == 429  # пока бюджет пуст, не пускает и верный пароль
    assert neighbour.status_code == 200  # чужой аккаунт не задет


async def test_successful_logins_do_not_spend_the_budget(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_login_account=3, auth_login_ip=100) as (
        _,
        http,
    ):
        user = await verified_user(http, jobs)
        statuses = [
            (
                await http.post(
                    LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
                )
            ).status_code
            for _ in range(10)
        ]

    assert statuses == [200] * 10


async def test_email_and_username_share_one_budget(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_login_account=3, auth_login_ip=100) as (
        _,
        http,
    ):
        user = await verified_user(http, jobs)
        email, username = user.credentials["email"], user.credentials["username"]

        statuses = [
            (await wrong_login(http, login)).status_code
            for login in (email, username, email.upper(), username)
        ]

    assert statuses == [401, 401, 401, 429]  # чередуя логины, бюджет не обойти


async def test_an_unknown_login_is_limited_exactly_like_a_known_one(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_login_account=2, auth_login_ip=100) as (
        _,
        http,
    ):
        user = await verified_user(http, jobs)
        for login in (user.credentials["email"], "ghost@example.com"):
            await wrong_login(http, login)
            await wrong_login(http, login)
        known = await wrong_login(http, user.credentials["email"])
        unknown = await wrong_login(http, "ghost@example.com")

    assert (known.status_code, unknown.status_code) == (429, 429)
    assert shape(known) == shape(unknown)  # по ответу не отличить, есть ли такой аккаунт


async def test_the_account_budget_comes_back_with_time(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(
        test_settings,
        jobs,
        auth_login_account=2,
        windows={"auth_login_account": 2},
        auth_login_ip=100,
    ) as (_, http):
        user = await verified_user(http, jobs)
        email = user.credentials["email"]
        await wrong_login(http, email)
        await wrong_login(http, email)
        assert (await wrong_login(http, email)).status_code == 429

        await asyncio.sleep(1.2)  # токен возвращается раз в секунду

        assert (await wrong_login(http, email)).status_code == 401


async def test_password_confirmation_spends_the_same_budget_as_login(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    """Подбор пароля через украденный access-токен упирается в тот же лимит, что и вход."""
    async with limited_client(test_settings, jobs, auth_login_account=3, auth_login_ip=100) as (
        _,
        http,
    ):
        user = await verified_user(http, jobs)
        attempts = [
            (
                await http.post(
                    CHANGE,
                    json={
                        "current_password": f"guess number {n}",
                        "new_password": "fresh passphrase 99",
                    },
                    headers=user.headers,
                )
            )
            for n in range(4)
        ]
        login = await http.post(
            LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
        )

    assert [a.status_code for a in attempts] == [403, 403, 403, 429]
    assert login.status_code == 429  # и вход по паролю тоже закрыт, бюджет общий


# ----------------------------------------------------------------------------- регистрация и письма
async def test_registration_is_limited_per_address_and_counts_invalid_requests(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_register_ip=3) as (_, http):
        weak = await http.post(REGISTER, json=new_credentials(password="qwertyuiop"))
        valid = await http.post(REGISTER, json=new_credentials())
        broken = await http.post(REGISTER, json={})
        refused = await http.post(REGISTER, json=new_credentials())

    assert (weak.status_code, valid.status_code, broken.status_code) == (422, 201, 422)
    assert refused.status_code == 429


async def test_the_limit_is_checked_before_the_body_is_validated(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_register_ip=1) as (_, http):
        first = await http.post(REGISTER, json={})
        second = await http.post(REGISTER, json={})

    assert (first.status_code, second.status_code) == (422, 429)


async def test_resend_is_limited_per_mailbox_whatever_the_case_or_endpoint(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_email_addr=3, auth_email_ip=100) as (
        _,
        http,
    ):
        results = [
            await http.post(RESEND, json={"email": "Person@Example.com"}),
            await http.post(RESEND, json={"email": "person@example.com"}),
            await http.post(FORGOT, json={"email": "PERSON@EXAMPLE.COM"}),  # тот же бюджет адреса
            await http.post(RESEND, json={"email": "person@example.com"}),
        ]
        another = await http.post(RESEND, json={"email": "someone-else@example.com"})

    assert [r.status_code for r in results] == [202, 202, 202, 429]
    assert another.status_code == 202


async def test_mail_requests_are_limited_per_address_of_the_client_too(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_email_ip=3, auth_email_addr=100) as (
        _,
        http,
    ):
        results = [
            await http.post(RESEND, json={"email": f"person-{n}@example.com"}) for n in range(4)
        ]

    assert [r.status_code for r in results] == [202, 202, 202, 429]


async def test_an_unknown_address_is_limited_like_a_known_one(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_email_addr=1, auth_email_ip=100) as (
        _,
        http,
    ):
        user = await verified_user(http, jobs)
        known = [
            await http.post(FORGOT, json={"email": user.credentials["email"]}) for _ in range(2)
        ]
        unknown = [await http.post(FORGOT, json={"email": "ghost@example.com"}) for _ in range(2)]

    assert [r.status_code for r in known] == [202, 429]
    assert [r.status_code for r in unknown] == [202, 429]
    assert shape(known[1]) == shape(unknown[1])


TOKEN_ENDPOINTS = [
    ("/api/v1/auth/verify-email", {"token": "x" * 43}),
    ("/api/v1/auth/password/reset", {"token": "x" * 43, "new_password": NEW_PASSWORD}),
    ("/api/v1/auth/email/confirm", {"token": "x" * 43}),
]


@pytest.mark.parametrize(("path", "body"), TOKEN_ENDPOINTS)
async def test_endpoints_with_a_mail_token_are_limited_per_address(
    test_settings: Settings, jobs: InMemoryJobQueue, path: str, body: dict[str, str]
) -> None:
    async with limited_client(test_settings, jobs, auth_email_ip=2) as (_, http):
        answers = [await http.post(path, json=body) for _ in range(3)]

    assert [a.status_code for a in answers] == [400, 400, 429]
    assert answers[0].json()["code"] == "token_invalid_or_expired"
    assert answers[0].headers["ratelimit-limit"] == "2"  # и ответ-ошибка показывает остаток
    assert answers[2].json()["code"] == "rate_limited"
    assert answers[2].headers["retry-after"]


async def test_mail_token_endpoints_and_mail_requests_share_one_budget_per_address(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    (verify_path, verify_body), (reset_path, reset_body), (confirm_path, confirm_body) = (
        TOKEN_ENDPOINTS
    )
    async with limited_client(test_settings, jobs, auth_email_ip=4, auth_email_addr=100) as (
        _,
        http,
    ):
        results = [
            await http.post(RESEND, json={"email": "a@example.com"}),
            await http.post(FORGOT, json={"email": "b@example.com"}),
            await http.post(verify_path, json=verify_body),
            await http.post(reset_path, json=reset_body),
            await http.post(confirm_path, json=confirm_body),
        ]

    assert [r.status_code for r in results] == [202, 202, 400, 400, 429]


# ----------------------------------------------------------------------------- прочие бакеты
async def test_username_checks_are_limited_per_address(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, username_check_ip=3) as (_, http):
        answers = [await http.get(USERNAME, params={"username": f"name_{n}"}) for n in range(4)]

    assert [a.status_code for a in answers] == [200, 200, 200, 429]
    assert answers[0].headers["ratelimit-remaining"] == "2"


async def test_reads_are_limited_per_user(test_settings: Settings, jobs: InMemoryJobQueue) -> None:
    async with limited_client(test_settings, jobs, api_read=3) as (_, http):
        alice = await verified_user(http, jobs)
        bob = await verified_user(http, jobs)
        alice_answers = [await http.get(ME, headers=alice.headers) for _ in range(4)]
        bob_answer = await http.get(ME, headers=bob.headers)

    assert [a.status_code for a in alice_answers] == [200, 200, 200, 429]
    assert [a.headers["ratelimit-remaining"] for a in alice_answers[:3]] == ["2", "1", "0"]
    assert bob_answer.status_code == 200


async def test_refresh_is_limited_per_session(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    async with limited_client(test_settings, jobs, auth_refresh_session=2) as (_, http):
        user = await verified_user(http, jobs)
        other = await verified_user(http, jobs)
        first = await do_refresh(http, user.refresh_token)
        second = await do_refresh(http, user.refresh_token)  # окно гонки: запрос тоже считается
        third = await do_refresh(http, user.refresh_token)
        neighbour = await do_refresh(http, other.refresh_token)

    assert [r.status_code for r in (first, second, third)] == [200, 200, 429]
    assert third.headers["retry-after"]
    assert third.json()["code"] == "rate_limited"
    assert neighbour.status_code == 200  # лимит по сессии: чужая сессия не задета


async def test_a_request_rejected_by_csrf_spends_no_refresh_budget(
    test_settings: Settings, jobs: InMemoryJobQueue
) -> None:
    """Проверка CSRF идёт до лимитов: чужой сайт не может израсходовать бюджет сессии."""
    async with limited_client(test_settings, jobs, auth_refresh_session=1) as (_, http):
        user = await verified_user(http, jobs)
        forged = await http.post(REFRESH, headers=cookie_header(user.refresh_token))
        ok = await http.post(REFRESH, headers=refresh_headers(user.refresh_token))

    assert (forged.status_code, ok.status_code) == (403, 200)
    assert forged.json()["code"] == "csrf_failed"


# ----------------------------------------------------------------------------- сбой Redis
async def test_closed_buckets_answer_503_and_open_ones_keep_working_without_redis(
    test_settings: Settings, jobs: InMemoryJobQueue, client: httpx.AsyncClient
) -> None:
    user: SignedInUser = await verified_user(client, jobs)  # аккаунт создаём в здоровом приложении
    broken = test_settings.model_copy(
        update={"redis_url": SecretStr("redis://127.0.0.1:1/0"), "rate_limits_enabled": True}
    )
    application = create_app(broken, job_queue=jobs)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            login = await http.post(
                LOGIN, json={"login": user.credentials["email"], "password": PASSWORD}
            )
            register = await http.post(REGISTER, json=new_credentials())
            resend = await http.post(RESEND, json={"email": "person@example.com"})
            username = await http.get(USERNAME, params={"username": "free_name"})
            me = await http.get(ME, headers=user.headers)

    # Подбор паролей и рассылка без счёта попыток не нужны: бакеты закрываются.
    for closed in (login, register, resend):
        assert closed.status_code == 503
        assert closed.json()["code"] == "service_unavailable"
        assert closed.headers["retry-after"] == "5"
    # Чтение и проверка ника продолжают работать (с предупреждением в журнале).
    assert username.status_code == 200
    assert me.status_code == 200
