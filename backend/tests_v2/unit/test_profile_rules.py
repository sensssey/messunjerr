"""Правила профиля: возраст, дата рождения, ссылки (5.3)."""

from datetime import date

import pytest

from messunjerr.profiles.domain.rules import (
    MAX_PLAUSIBLE_AGE,
    BirthDateProblem,
    age_on,
    check_birth_date,
    is_valid_link_url,
)

TODAY = date(2026, 10, 5)


@pytest.mark.parametrize(
    ("born", "today", "age"),
    [
        (date(2000, 10, 5), date(2026, 10, 5), 26),  # день рождения сегодня: уже 26
        (date(2000, 10, 6), date(2026, 10, 5), 25),  # завтра: ещё 25
        (date(2000, 10, 4), date(2026, 10, 5), 26),
        (date(2000, 1, 1), date(2026, 12, 31), 26),
        (date(2000, 12, 31), date(2026, 1, 1), 25),
        (
            date(2008, 2, 29),
            date(2026, 2, 28),
            17,
        ),  # 29 февраля: до 1 марта в невисокосный год ещё нет
        (date(2008, 2, 29), date(2026, 3, 1), 18),
        (date(2008, 2, 29), date(2028, 2, 29), 20),
        (date(2026, 10, 5), date(2026, 10, 5), 0),
    ],
)
def test_age_counts_full_years(born: date, today: date, age: int) -> None:
    assert age_on(born, today) == age


def test_the_minimum_age_boundary_is_the_birthday() -> None:
    assert check_birth_date(date(2008, 10, 5), today=TODAY, min_age=18) is None
    assert check_birth_date(date(2008, 10, 6), today=TODAY, min_age=18) is BirthDateProblem.UNDERAGE


def test_future_dates_are_not_dates_of_birth() -> None:
    assert check_birth_date(date(2026, 10, 6), today=TODAY, min_age=18) is BirthDateProblem.FUTURE
    assert check_birth_date(date(2999, 1, 1), today=TODAY, min_age=0) is BirthDateProblem.FUTURE


def test_implausibly_old_dates_are_rejected() -> None:
    year = TODAY.year - MAX_PLAUSIBLE_AGE - 1
    oldest_accepted = date(year, TODAY.month, TODAY.day + 1)  # завтра исполнится 121, пока 120
    assert check_birth_date(oldest_accepted, today=TODAY, min_age=18) is None
    too_old = date(year, TODAY.month, TODAY.day)  # сегодня исполнился 121 год
    assert check_birth_date(too_old, today=TODAY, min_age=18) is BirthDateProblem.IMPLAUSIBLE


def test_a_zero_minimum_age_accepts_a_newborn() -> None:
    assert check_birth_date(TODAY, today=TODAY, min_age=0) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?q=1#frag",
        "https://sub.example.co.uk:8443/a/b",
        "https://пример.рф/страница",
        "HTTPS://EXAMPLE.COM",
        "http://127.0.0.1:8000/x",
        "http://[::1]/x",
    ],
)
def test_http_and_https_links_are_accepted(url: str) -> None:
    assert is_valid_link_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "//example.com",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "mailto:user@example.com",
        "ftp://example.com",
        "file:///etc/passwd",
        "https://",
        "https:///path",
        "https://user:pass@example.com",
        "https://user@example.com",
        "https://example.com/a b",
        "https://example.com/\n",
        "https://exa\tmple.com",
        "https://example.com:99999",
        "https://example.com:port",
        "https://[::1",
        "https://example.com/​",  # невидимый символ
    ],
)
def test_everything_else_is_not_a_profile_link(url: str) -> None:
    assert not is_valid_link_url(url)
