"""Помощники тестов поиска людей (S8-04): люди напрямую в БД, запрос поиска и порядок выдачи.

Люди создаются одним запросом к БД (в обход регистрации): тестам поиска нужны точные ники и имена,
а не случайные `user_<hex>`. Зритель входит обычным путём, чтобы у него был настоящий токен.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .helpers import SignedInUser

SEARCH = "/api/v1/search/users"


@dataclass(frozen=True, slots=True)
class Person:
    """Человек для поиска: ник и отображаемое имя; остальное по умолчанию (активный, открытый)."""

    username: str
    name: str
    status: str = "active"
    private: bool = False
    bio: str | None = None
    city: str | None = None


async def add_people(engine: AsyncEngine, people: Sequence[Person]) -> dict[str, uuid.UUID]:
    """Аккаунты и профили одним запросом; возвращает идентификаторы по никам."""
    ids = {person.username: uuid.uuid4() for person in people}
    users = list(people)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO identity.users (id, email, username, terms_version, terms_accepted_at, "
                "status) SELECT t.id, t.username || '@example.com', t.username, 'v', now(), t.status "
                "FROM unnest(CAST(:ids AS uuid[]), CAST(:usernames AS text[]), "
                "CAST(:statuses AS text[])) AS t(id, username, status)"
            ),
            {
                "ids": [ids[p.username] for p in users],
                "usernames": [p.username for p in users],
                "statuses": [p.status for p in users],
            },
        )
        await connection.execute(
            text(
                "INSERT INTO profile.profiles (user_id, display_name, is_private, bio, city) "
                "SELECT t.id, t.name, t.private, t.bio, t.city FROM unnest(CAST(:ids AS uuid[]), "
                "CAST(:names AS text[]), CAST(:privates AS boolean[]), CAST(:bios AS text[]), "
                "CAST(:cities AS text[])) AS t(id, name, private, bio, city)"
            ),
            {
                "ids": [ids[p.username] for p in users],
                "names": [p.name for p in users],
                "privates": [p.private for p in users],
                "bios": [p.bio for p in users],
                "cities": [p.city for p in users],
            },
        )
    return ids


async def add_person(engine: AsyncEngine, username: str, name: str, **fields: Any) -> uuid.UUID:
    return (await add_people(engine, [Person(username, name, **fields)]))[username]


async def set_person_status(engine: AsyncEngine, person: uuid.UUID, status: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE identity.users SET status = :status WHERE id = :id"),
            {"status": status, "id": person},
        )


async def search(
    client: httpx.AsyncClient, viewer: SignedInUser, q: str, **params: Any
) -> httpx.Response:
    return await client.get(SEARCH, params={"q": q, **params}, headers=viewer.headers)


def usernames(response: httpx.Response) -> list[str]:
    """Ники найденных людей в порядке выдачи; ответ обязан быть успешным."""
    assert response.status_code == 200, response.text
    return [item["user"]["username"] for item in response.json()["items"]]


def display_names(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["user"]["display_name"] for item in response.json()["items"]]


async def walk(
    client: httpx.AsyncClient, viewer: SignedInUser, q: str, *, limit: int = 50
) -> list[str]:
    """Все ники по страницам: идёт по `next_offset`, пока он есть, и просит не больше, чем осталось до потолка."""
    found: list[str] = []
    offset = 0
    while True:
        response = await search(client, viewer, q, limit=min(limit, 200 - offset), offset=offset)
        assert response.status_code == 200, response.text
        page = response.json()
        found.extend(item["user"]["username"] for item in page["items"])
        if page["next_offset"] is None:
            return found
        offset = page["next_offset"]
