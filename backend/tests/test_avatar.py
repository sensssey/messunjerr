import io
import os

import pytest
from PIL import Image
from sqlalchemy import func, select

from src.database import SessionLocal
from src.models.avatars import AvatarDB
from src.profile import routes as avatar_routes


def make_image(image_format, mode="RGB", size=(900, 700), **save_kwargs):
    color = (70, 110, 200, 128) if mode == "RGBA" else (70, 110, 200)
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


async def upload(client, headers, data, content_type="image/png"):
    return await client.post("/users/avatar", files={"file": ("avatar", data, content_type)}, headers=headers)


async def test_upload_png_resizes_and_serves_it_back(client, make_user):
    alice = await make_user("alice")
    response = await upload(client, alice, make_image("PNG", size=(800, 400)))
    assert response.status_code == 201
    assert response.json() == {"message": "Avatar uploaded and resized successfully"}

    avatar = await client.get("/users/avatar", headers=alice)
    assert avatar.status_code == 200
    assert avatar.headers["content-type"] == "image/png"
    assert "avatar_1" in avatar.headers["content-disposition"]
    image = Image.open(io.BytesIO(avatar.content))
    assert image.format == "PNG"
    assert image.size == (400, 200)  # пропорции сохраняются


async def test_upload_jpeg(client, make_user):
    alice = await make_user("alice")
    assert (await upload(client, alice, make_image("JPEG"), "image/jpeg")).status_code == 201
    avatar = await client.get("/users/avatar", headers=alice)
    assert avatar.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(avatar.content)).size) <= 400


async def test_format_is_detected_from_content_not_from_declared_type(client, make_user):
    alice = await make_user("alice")
    # PNG с прозрачностью, заявленный как JPEG: раньше это заканчивалось ошибкой 500
    response = await upload(client, alice, make_image("PNG", mode="RGBA"), "image/jpeg")
    assert response.status_code == 201
    avatar = await client.get("/users/avatar", headers=alice)
    assert avatar.headers["content-type"] == "image/png"


async def test_avatar_url_in_profile_follows_upload_and_delete(client, make_user):
    alice = await make_user("alice")
    assert (await client.get("/auth/users/me", headers=alice)).json()["avatar_url"] is None

    await upload(client, alice, make_image("PNG"))
    assert (await client.get("/auth/users/me", headers=alice)).json()["avatar_url"] == "/users/avatar"

    assert (await client.delete("/users/avatar", headers=alice)).status_code == 204
    assert (await client.get("/auth/users/me", headers=alice)).json()["avatar_url"] is None


async def test_delete_avatar(client, make_user):
    alice = await make_user("alice")
    await upload(client, alice, make_image("PNG"))
    assert (await client.delete("/users/avatar", headers=alice)).status_code == 204
    assert (await client.get("/users/avatar", headers=alice)).status_code == 404
    assert (await client.delete("/users/avatar", headers=alice)).status_code == 404


async def test_uploading_again_replaces_the_avatar(client, make_user):
    alice = await make_user("alice")
    await upload(client, alice, make_image("PNG", size=(100, 100)))
    await upload(client, alice, make_image("PNG", size=(50, 80)))
    async with SessionLocal() as session:
        assert (await session.execute(select(func.count()).select_from(AvatarDB))).scalar_one() == 1
    avatar = await client.get("/users/avatar", headers=alice)
    assert Image.open(io.BytesIO(avatar.content)).size == (50, 80)


async def test_users_do_not_see_each_others_avatars(client, make_user):
    alice, bob = await make_user("alice"), await make_user("bob")
    await upload(client, alice, make_image("PNG"))
    assert (await client.get("/users/avatar", headers=bob)).status_code == 404


async def test_rejects_file_that_is_not_an_image(client, make_user):
    alice = await make_user("alice")
    response = await upload(client, alice, b"definitely not an image", "image/png")
    assert response.status_code == 400
    # внутренности исключения наружу не попадают
    assert response.json() == {"detail": "Invalid image file"}


async def test_rejects_other_image_formats(client, make_user):
    alice = await make_user("alice")
    response = await upload(client, alice, make_image("GIF"), "image/gif")
    assert response.status_code == 400
    assert response.json() == {"detail": "Only JPEG/PNG allowed"}


async def test_rejects_too_large_upload(client, make_user, monkeypatch):
    monkeypatch.setattr(avatar_routes, "AVATAR_MAX_BYTES", 1024)
    alice = await make_user("alice")
    response = await upload(client, alice, os.urandom(2048))
    assert response.status_code == 413


async def test_rejects_image_with_too_many_pixels(client, make_user, monkeypatch):
    monkeypatch.setattr(avatar_routes, "MAX_PIXELS", 50 * 50)
    alice = await make_user("alice")
    response = await upload(client, alice, make_image("PNG", size=(100, 100)))
    assert response.status_code == 400
    assert response.json() == {"detail": "Image is too large"}


async def test_exif_orientation_is_applied_and_metadata_is_dropped(client, make_user):
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: повернуть на 90°
    exif[0x010F] = "TestCamera"  # Make
    alice = await make_user("alice")
    response = await upload(client, alice, make_image("JPEG", size=(200, 100), exif=exif.tobytes()), "image/jpeg")
    assert response.status_code == 201

    result = Image.open(io.BytesIO((await client.get("/users/avatar", headers=alice)).content))
    assert result.size == (100, 200)
    assert 0x010F not in result.getexif()


async def test_avatar_is_stored_compactly_as_base64(client, make_user):
    alice = await make_user("alice")
    await upload(client, alice, make_image("JPEG", size=(300, 300)), "image/jpeg")
    served = await client.get("/users/avatar", headers=alice)
    async with SessionLocal() as session:
        stored = (await session.execute(select(AvatarDB.file_data))).scalar_one()
    assert stored.startswith("b64:")
    assert len(stored) < len(served.content) * 1.4 + 8


async def test_avatar_in_legacy_storage_format_is_still_readable(client, make_user):
    alice = await make_user("alice")
    original = make_image("PNG", size=(64, 64))
    # так прежняя версия кода записывала байты в текстовую колонку
    legacy = original.decode("latin1").encode("unicode-escape").decode("ascii")
    async with SessionLocal() as session:
        session.add(AvatarDB(user_id=1, file_data=legacy))
        await session.commit()

    response = await client.get("/users/avatar", headers=alice)
    assert response.status_code == 200
    assert response.content == original
    assert response.headers["content-type"] == "image/png"


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
async def test_avatar_endpoints_require_authentication(client, method):
    files = {"file": ("a.png", make_image("PNG"), "image/png")} if method == "POST" else None
    response = await client.request(method, "/users/avatar", files=files)
    assert response.status_code == 401
