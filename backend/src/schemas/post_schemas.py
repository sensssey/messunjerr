from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints


def _as_utc(value: datetime) -> datetime:
    """В БД лежит naive-UTC, клиенту отдаём время с таймзоной (суффикс Z)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]


class PostCreate(BaseModel):
    title: Title
    content: Content


class PostUpdate(BaseModel):
    title: Optional[Title] = None
    content: Optional[Content] = None


class Post(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    content: str
    created_at: UTCDateTime
    updated_at: UTCDateTime
