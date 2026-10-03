import base64
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from src.auth.auth import get_current_user
from src.config import AVATAR_MAX_BYTES
from src.database import get_db
from src.models.avatars import AvatarDB
from src.models.models import UserDB

logger = logging.getLogger(__name__)

profile_router = APIRouter(prefix="/users", tags=["Users"])

MAX_SIZE = (400, 400)
MAX_PIXELS = 25_000_000
ALLOWED_FORMATS = ("JPEG", "PNG")
# Новые аватары лежат в БД как "b64:<base64>". Старый формат (latin1 + unicode-escape) по-прежнему читается
B64_PREFIX = "b64:"


def encode_avatar(binary_data: bytes) -> str:
    return B64_PREFIX + base64.b64encode(binary_data).decode("ascii")


def decode_avatar(stored: str) -> bytes:
    if stored.startswith(B64_PREFIX):
        return base64.b64decode(stored[len(B64_PREFIX):])
    return stored.encode("ascii").decode("unicode-escape").encode("latin1")


def process_avatar(contents: bytes) -> bytes:
    """Проверяет картинку по содержимому, уменьшает до MAX_SIZE и перекодирует (метаданные не сохраняются)."""
    try:
        image = Image.open(io.BytesIO(contents))
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid image file")
    if image.format not in ALLOWED_FORMATS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Only JPEG/PNG allowed")
    if image.width * image.height > MAX_PIXELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Image is too large")

    image_format = image.format
    try:
        image = ImageOps.exif_transpose(image)
        image.thumbnail(MAX_SIZE)
        img_byte_arr = io.BytesIO()
        if image_format == "JPEG":
            image.save(img_byte_arr, format="JPEG", quality=85)
        else:
            image.save(img_byte_arr, format="PNG", optimize=True)
    except Exception:
        logger.warning("Avatar processing failed", exc_info=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid image file")
    return img_byte_arr.getvalue()


@profile_router.post("/avatar", status_code=status.HTTP_201_CREATED)
async def upload_avatar(
        file: UploadFile = File(...),
        current_user: UserDB = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    too_large = HTTPException(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"File is too large (max {AVATAR_MAX_BYTES // 1024} KB)"
    )
    if file.size is not None and file.size > AVATAR_MAX_BYTES:
        raise too_large
    contents = await file.read(AVATAR_MAX_BYTES + 1)
    if len(contents) > AVATAR_MAX_BYTES:
        raise too_large

    binary_data = await run_in_threadpool(process_avatar, contents)
    file_data_str = encode_avatar(binary_data)

    avatar = await db.execute(
        select(AvatarDB).where(AvatarDB.user_id == current_user.id)
    )
    avatar = avatar.scalar_one_or_none()
    if avatar:
        avatar.file_data = file_data_str
    else:
        avatar = AvatarDB(user_id=current_user.id, file_data=file_data_str)
        db.add(avatar)
    await db.commit()
    return {"message": "Avatar uploaded and resized successfully"}


@profile_router.get("/avatar")
async def get_avatar(
        current_user: UserDB = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AvatarDB.file_data).where(AvatarDB.user_id == current_user.id)
    )
    file_data = result.scalar_one_or_none()

    if not file_data:
        raise HTTPException(status_code=404, detail="Avatar not found")

    try:
        binary_data = decode_avatar(file_data)
    except Exception:
        logger.exception("Failed to decode avatar of user %s", current_user.id)
        raise HTTPException(500, detail="Failed to process avatar")

    if binary_data.startswith(b'\xff\xd8'):
        media_type = "image/jpeg"
    elif binary_data.startswith(b'\x89PNG'):
        media_type = "image/png"
    else:
        media_type = "application/octet-stream"

    return Response(
        content=binary_data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename=avatar_{current_user.id}"
        }
    )

@profile_router.delete("/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def delete_avatar(
        current_user: UserDB = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    avatar = await db.execute(
        select(AvatarDB).where(AvatarDB.user_id == current_user.id)
    )
    avatar = avatar.scalar_one_or_none()
    if not avatar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Avatar not found"
        )
    await db.delete(avatar)
    await db.commit()
    return None
