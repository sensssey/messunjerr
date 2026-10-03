from datetime import datetime, timezone

import pytest

from src.database import SessionLocal
from src.models.post_models import Post


async def create(client, headers, title="Hello", content="World"):
    response = await client.post("/posts/", json={"title": title, "content": content}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_post_crud_flow(client, make_user):
    alice = await make_user("alice")
    post = await create(client, alice)
    assert post["title"] == "Hello"
    assert post["content"] == "World"
    assert post["user_id"] == 1

    listed = await client.get("/posts/", headers=alice)
    assert [p["id"] for p in listed.json()] == [post["id"]]

    fetched = await client.get(f"/posts/{post['id']}", headers=alice)
    assert fetched.status_code == 200
    assert fetched.json() == post

    # PUT обновляет только переданные поля
    updated = await client.put(f"/posts/{post['id']}", json={"title": "New title"}, headers=alice)
    assert updated.status_code == 200
    assert updated.json()["title"] == "New title"
    assert updated.json()["content"] == "World"
    assert updated.json()["updated_at"] >= post["updated_at"]

    deleted = await client.delete(f"/posts/{post['id']}", headers=alice)
    assert deleted.status_code == 200
    assert (await client.get(f"/posts/{post['id']}", headers=alice)).status_code == 404
    assert (await client.get("/posts/", headers=alice)).json() == []


async def test_posts_are_listed_newest_first(client, make_user):
    alice = await make_user("alice")
    for title in ("first", "second", "third"):
        await create(client, alice, title=title)
    titles = [p["title"] for p in (await client.get("/posts/", headers=alice)).json()]
    assert titles == ["third", "second", "first"]


async def test_list_supports_limit_and_offset(client, make_user):
    alice = await make_user("alice")
    for number in range(5):
        await create(client, alice, title=f"post {number}")

    first_page = await client.get("/posts/", params={"limit": 2}, headers=alice)
    assert [p["title"] for p in first_page.json()] == ["post 4", "post 3"]
    second_page = await client.get("/posts/", params={"limit": 2, "offset": 2}, headers=alice)
    assert [p["title"] for p in second_page.json()] == ["post 2", "post 1"]
    # без параметров отдаются все посты
    assert len((await client.get("/posts/", headers=alice)).json()) == 5

    for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert (await client.get("/posts/", params=params, headers=alice)).status_code == 422


@pytest.mark.parametrize("payload", [
    {},
    {"title": "only title"},
    {"content": "only content"},
    {"title": "   ", "content": "c"},
    {"title": "t", "content": ""},
    {"title": "x" * 201, "content": "c"},
    {"title": "t", "content": "x" * 10001},
])
async def test_create_post_validates_input(client, make_user, payload):
    alice = await make_user("alice")
    response = await client.post("/posts/", json=payload, headers=alice)
    assert response.status_code == 422


async def test_update_post_validates_input(client, make_user):
    alice = await make_user("alice")
    post = await create(client, alice)
    response = await client.put(f"/posts/{post['id']}", json={"title": "  "}, headers=alice)
    assert response.status_code == 422


async def test_posts_of_other_users_are_not_accessible(client, make_user):
    alice, bob = await make_user("alice"), await make_user("bob")
    post = await create(client, alice)

    assert (await client.get("/posts/", headers=bob)).json() == []
    assert (await client.get(f"/posts/{post['id']}", headers=bob)).status_code == 403
    assert (await client.put(f"/posts/{post['id']}", json={"title": "pwned"}, headers=bob)).status_code == 403
    assert (await client.delete(f"/posts/{post['id']}", headers=bob)).status_code == 403

    unchanged = await client.get(f"/posts/{post['id']}", headers=alice)
    assert unchanged.json()["title"] == "Hello"


async def test_posts_require_authentication(client, make_user):
    alice = await make_user("alice")
    post = await create(client, alice)
    body = {"title": "t", "content": "c"}

    for method, path in (
        ("GET", "/posts/"),
        ("POST", "/posts/"),
        ("GET", f"/posts/{post['id']}"),
        ("PUT", f"/posts/{post['id']}"),
        ("DELETE", f"/posts/{post['id']}"),
    ):
        response = await client.request(method, path, json=body if method in ("POST", "PUT") else None)
        assert response.status_code == 401, f"{method} {path}"


async def test_missing_post_returns_404(client, make_user):
    alice = await make_user("alice")
    assert (await client.get("/posts/999", headers=alice)).status_code == 404
    assert (await client.put("/posts/999", json={"title": "x"}, headers=alice)).status_code == 404
    assert (await client.delete("/posts/999", headers=alice)).status_code == 404


async def test_timestamps_are_utc_and_set_by_server(client, make_user):
    alice = await make_user("alice")
    response = await client.post(
        "/posts/",
        json={"title": "t", "content": "c", "created_at": "1999-01-01T00:00:00"},
        headers=alice,
    )
    post = response.json()
    # суффикс Z: клиент однозначно понимает, что это UTC
    assert post["created_at"].endswith("Z")
    created = datetime.fromisoformat(post["created_at"])
    assert abs((datetime.now(timezone.utc) - created).total_seconds()) < 60


async def test_model_fills_timestamps_by_default(make_user):
    await make_user("alice")
    async with SessionLocal() as session:
        post = Post(user_id=1, title="t", content="c")
        session.add(post)
        await session.commit()
        await session.refresh(post)
        assert post.created_at is not None
        assert post.updated_at is not None
