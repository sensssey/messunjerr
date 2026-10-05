"""Модели чтения контекста identity: неизменяемые, формат из спецификации (5.3)."""

import uuid

from pydantic import BaseModel, ConfigDict

from messunjerr.core.schemas import UtcDateTime
from messunjerr.identity.infra.models import UserRow


class MeUser(BaseModel):
    """Текущий пользователь. Профиль, приватность и счётчики добавятся в S3 (план спринтов S3-04)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    username: str
    email: str
    email_verified: bool
    role: str
    status: str
    created_at: UtcDateTime

    @classmethod
    def from_row(cls, user: UserRow) -> "MeUser":
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            email_verified=user.email_verified_at is not None,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
        )
