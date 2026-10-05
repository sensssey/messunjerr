"""Репозитории profiles: тонкая обёртка над `AsyncSession`. `commit()` они не вызывают (4.3)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.profiles.infra.models import PrivacySettingsRow, ProfileRow


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> ProfileRow | None:
        statement = select(ProfileRow).where(ProfileRow.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def upsert_for_registration(
        self,
        user_id: uuid.UUID,
        *,
        display_name: str,
        language: str | None,
        timezone: str | None,
    ) -> None:
        """Профиль нового аккаунта; при повторной регистрации неподтверждённого адреса данные регистрации
        заменяют прежние (идентичность и профиль принадлежат тому, кто подтвердит почту)."""
        statement = pg_insert(ProfileRow).values(
            user_id=user_id, display_name=display_name, language=language, timezone=timezone
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[ProfileRow.user_id],
                set_={
                    "display_name": display_name,
                    "language": language,
                    "timezone": timezone,
                    "updated_at": func.now(),
                },
            )
        )


class PrivacyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, user_id: uuid.UUID, *, for_update: bool = False
    ) -> PrivacySettingsRow | None:
        statement = select(PrivacySettingsRow).where(PrivacySettingsRow.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def create_defaults(self, user_id: uuid.UUID) -> None:
        """Настройки по умолчанию; существующие не трогает."""
        await self._session.execute(
            pg_insert(PrivacySettingsRow)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=[PrivacySettingsRow.user_id])
        )
