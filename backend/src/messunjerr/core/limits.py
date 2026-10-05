"""Лимиты контента: единый источник для валидации и для `GET /api/v1/meta` (5.13)."""

from dataclasses import asdict, dataclass

from messunjerr.settings import Settings

MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Limits:
    bio_max: int = 500
    links_max: int = 5
    post_body_max: int = 5000
    comment_body_max: int = 2000
    message_body_max: int = 4000
    post_media_max: int = 10
    message_attachments_max: int = 10
    avatar_max_bytes: int = 5 * MIB
    image_max_bytes: int = 10 * MIB
    file_max_bytes: int = 25 * MIB


LIMITS = Limits()
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128


def limits_for_meta(settings: Settings) -> dict[str, int]:
    return {
        **asdict(LIMITS),
        "group_members_max": settings.group_max_members,
        "quota_bytes": settings.media_quota_bytes,
        "message_edit_window_hours": settings.message_edit_window_hours,
    }
