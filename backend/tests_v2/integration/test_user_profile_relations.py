"""`GET /users/{ref}` при настоящих отношениях: друг, подписчик, блокировка, счётчики из порта.

Социального графа до S7–S8 нет, поэтому порты `Relationships` и `ProfileCounters` подменяются двойниками.
Так через настоящую ручку проходят ветки, которые заглушки не достигают, и S7 подключает граф к уже
проверенному запросу, а не пишет его заново.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.jobs import InMemoryJobQueue
from messunjerr.profiles.domain.ports import ProfileCounts, RelationshipView
from messunjerr.profiles.services import ProfileServices

from .helpers import SignedInUser, set_privacy, two_users, verified_user, view

COUNTS = ProfileCounts(posts=5, friends=7, followers=9, following=3)


@dataclass
class FakeRelationships:
    """Отношение зрителя к владельцу задаётся по владельцу; остальные для зрителя «чужие»."""

    views: dict[uuid.UUID, RelationshipView] = field(
        default_factory=dict[uuid.UUID, RelationshipView]
    )

    async def between(
        self, session: AsyncSession, *, viewer_id: uuid.UUID, owner_id: uuid.UUID
    ) -> RelationshipView:
        return self.views.get(owner_id, RelationshipView(is_self=viewer_id == owner_id))


class FakeCounters:
    async def of(self, session: AsyncSession, user_id: uuid.UUID) -> ProfileCounts:
        return COUNTS


@pytest.fixture
def graph(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> FakeRelationships:
    services: ProfileServices = app.state.profiles
    fake = FakeRelationships()
    monkeypatch.setattr(services, "relationships", fake)
    monkeypatch.setattr(services, "counters", FakeCounters())
    return fake


def relate(graph: FakeRelationships, owner: SignedInUser, **view: Any) -> None:
    graph.views[uuid.UUID(owner.user_id)] = RelationshipView(is_self=False, **view)


async def private_owner_and_viewer(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, **privacy: Any
) -> tuple[SignedInUser, SignedInUser]:
    owner, viewer = await two_users(client, jobs, is_private=True)
    if privacy:
        await set_privacy(client, owner, **privacy)
    return owner, viewer


# ----------------------------------------------------------------------------- друг
async def test_a_friend_sees_everything_of_a_private_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await private_owner_and_viewer(client, jobs)
    relate(graph, owner, friendship="friends")

    body = (await view(client, viewer, owner.user_id)).json()

    assert body["is_private"] is True
    assert body["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert (body["city"], body["language"], body["timezone"]) == ("Казань", "ru", "Europe/Moscow")
    assert body["birth_date"] == "1990-05-12"
    # Настройки по умолчанию («друзья») дают другу все счётчики.
    assert body["counters"] == {"posts": 5, "friends": 7, "followers": 9, "following": 3}
    assert body["relationship"]["friendship"] == "friends"
    assert body["relationship"]["is_self"] is False


async def test_a_friend_sees_the_birth_date_in_the_volume_the_owner_chose(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await two_users(client, jobs, birth_date_visibility="day_month")
    relate(graph, owner, friendship="friends")

    assert (await view(client, viewer, owner.user_id)).json()["birth_date"] == "05-12"


# ----------------------------------------------------------------------------- подписчик
async def test_a_follower_of_a_private_profile_sees_the_details_but_not_friends_only_counters(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await private_owner_and_viewer(client, jobs)
    relate(graph, owner, following="following")

    body = (await view(client, viewer, owner.user_id)).json()

    assert body["links"] == [{"title": "Блог", "url": "https://example.com"}]
    assert body["birth_date"] == "1990-05-12"
    assert body["counters"] == {"posts": 5, "friends": None, "followers": None, "following": None}
    assert body["relationship"]["following"] == "following"


async def test_a_follower_sees_the_counters_the_owner_opened_to_everyone(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await private_owner_and_viewer(
        client, jobs, friends_list_visibility="everyone", followers_list_visibility="everyone"
    )
    relate(graph, owner, following="following")

    counters = (await view(client, viewer, owner.user_id)).json()["counters"]

    assert counters == {"posts": 5, "friends": 7, "followers": 9, "following": 3}


async def test_a_pending_follow_request_does_not_make_a_follower(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await private_owner_and_viewer(client, jobs)
    relate(graph, owner, following="requested")

    body = (await view(client, viewer, owner.user_id)).json()

    assert body["links"] == []
    assert body["birth_date"] is None
    assert body["counters"]["posts"] is None
    assert body["relationship"]["following"] == "requested"


# ----------------------------------------------------------------------------- счётчики из порта
@pytest.mark.parametrize(
    ("friends_setting", "followers_setting", "expected"),
    [
        ("everyone", "everyone", {"posts": 5, "friends": 7, "followers": 9, "following": 3}),
        ("everyone", "only_me", {"posts": 5, "friends": 7, "followers": None, "following": None}),
        ("only_me", "everyone", {"posts": 5, "friends": None, "followers": 9, "following": 3}),
        ("friends", "friends", {"posts": 5, "friends": None, "followers": None, "following": None}),
    ],
)
async def test_a_stranger_gets_the_counters_from_the_port_as_the_settings_allow(
    client: httpx.AsyncClient,
    jobs: InMemoryJobQueue,
    graph: FakeRelationships,
    friends_setting: str,
    followers_setting: str,
    expected: dict[str, int | None],
) -> None:
    owner, viewer = await two_users(client, jobs)
    await set_privacy(
        client,
        owner,
        friends_list_visibility=friends_setting,
        followers_list_visibility=followers_setting,
    )

    counters = (await view(client, viewer, owner.user_id)).json()["counters"]

    assert counters == expected


async def test_the_owner_sees_every_counter_of_their_own_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner = await verified_user(client, jobs)
    await set_privacy(
        client, owner, friends_list_visibility="only_me", followers_list_visibility="only_me"
    )

    counters = (await view(client, owner, owner.user_id)).json()["counters"]

    assert counters == {"posts": 5, "friends": 7, "followers": 9, "following": 3}


# ----------------------------------------------------------------------------- блокировка
@pytest.mark.parametrize("who", ["blocked_by_owner", "blocked"])
async def test_a_block_in_either_direction_hides_the_profile(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships, who: str
) -> None:
    owner, viewer = await two_users(client, jobs)
    relate(graph, owner, **{who: True})

    response = await view(client, viewer, owner.user_id)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_block_looks_exactly_like_a_missing_user(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await two_users(client, jobs)
    relate(graph, owner, blocked_by_owner=True)

    blocked = await view(client, viewer, owner.credentials["username"])
    missing = await view(client, viewer, "nobody_at_all")

    def shape(response: httpx.Response) -> dict[str, Any]:
        body: dict[str, Any] = response.json()
        return {key: body[key] for key in ("type", "title", "status", "code", "detail")}

    assert shape(blocked) == shape(missing)


async def test_a_block_beats_a_friendship_that_the_graph_has_not_cleaned_up_yet(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await two_users(client, jobs)
    relate(graph, owner, friendship="friends", following="following", blocked_by_owner=True)

    assert (await view(client, viewer, owner.user_id)).status_code == 404


# ----------------------------------------------------------------------------- Relationship из порта
async def test_the_relationship_of_the_port_reaches_the_response_unchanged(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    owner, viewer = await two_users(client, jobs)
    request_id = uuid.uuid4()
    relate(
        graph,
        owner,
        friendship="request_received",
        friend_request_id=request_id,
        following="requested",
        follows_you=True,
    )

    body = (await view(client, viewer, owner.user_id)).json()

    assert body["relationship"] == {
        "is_self": False,
        "friendship": "request_received",
        "friend_request_id": str(request_id),
        "following": "requested",
        "follows_you": True,
        "blocked": False,
    }


async def test_other_people_stay_strangers_when_the_graph_knows_only_one_relation(
    client: httpx.AsyncClient, jobs: InMemoryJobQueue, graph: FakeRelationships
) -> None:
    first, viewer = await two_users(client, jobs, is_private=True)
    second = await verified_user(client, jobs)
    relate(graph, first, friendship="friends")

    friend_view = (await view(client, viewer, first.user_id)).json()
    stranger_view = (await view(client, viewer, second.user_id)).json()

    assert friend_view["relationship"]["friendship"] == "friends"
    assert stranger_view["relationship"]["friendship"] == "none"
