"""Службы контекста profiles, которые создаются один раз при старте процесса.

`ProfileServices` одновременно выполняет роль портов identity (`ProfileProvisioner`: создать профиль
при регистрации, `MeExtrasProvider`: разделы `MeUser`) и держит порты профилей к контекстам выше.
Пока тех контекстов нет, стоят заглушки; сборка в `messunjerr.main` заменит их по мере появления.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import MeExtras
from messunjerr.identity.api_public import ProfileSeed
from messunjerr.profiles.domain.ports import AvatarAssets, ProfileCounters, Relationships
from messunjerr.profiles.infra.repositories import PrivacyRepository, ProfileRepository
from messunjerr.profiles.infra.stubs import NoGraphYet, NoMediaYet, ZeroCounters
from messunjerr.profiles.queries.me import load_me_extras


@dataclass(slots=True)
class ProfileServices:
    avatars: AvatarAssets
    relationships: Relationships
    counters: ProfileCounters

    async def provision(
        self, session: AsyncSession, *, user_id: uuid.UUID, username: str, seed: ProfileSeed
    ) -> None:
        """Профиль и настройки приватности нового аккаунта (S3-01). Имя по умолчанию равно нику."""
        await ProfileRepository(session).upsert_for_registration(
            user_id,
            display_name=seed.display_name or username,
            language=seed.language,
            timezone=seed.timezone,
        )
        await PrivacyRepository(session).create_defaults(user_id)

    async def load(self, session: AsyncSession, user_id: uuid.UUID) -> MeExtras:
        return await load_me_extras(session, user_id)


def create_profile_services(
    *,
    avatars: AvatarAssets | None = None,
    relationships: Relationships | None = None,
    counters: ProfileCounters | None = None,
) -> ProfileServices:
    """Службы профилей; не заданный порт заменяется заглушкой до появления своего контекста."""
    return ProfileServices(
        avatars=avatars or NoMediaYet(),
        relationships=relationships or NoGraphYet(),
        counters=counters or ZeroCounters(),
    )
