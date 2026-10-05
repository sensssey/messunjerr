"""Службы контекста identity, которые создаются один раз при старте процесса."""

from dataclasses import dataclass

from redis.asyncio import Redis

from messunjerr.identity.infra.jwt_service import TokenService, create_token_service
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.identity.infra.ports import MeExtrasProvider, ProfileProvisioner
from messunjerr.identity.infra.session_denylist import SessionDenylist
from messunjerr.settings import Settings


@dataclass(slots=True)
class IdentityServices:
    passwords: PasswordService
    tokens: TokenService
    denylist: SessionDenylist
    me_extras: MeExtrasProvider
    """Профиль, приватность и счётчики для `MeUser`: их отдают контексты выше (порт, S3)."""
    provisioner: ProfileProvisioner
    """Создаёт профиль нового аккаунта при регистрации (порт, S3)."""

    def close(self) -> None:
        self.passwords.shutdown()


async def create_identity_services(
    settings: Settings,
    redis: Redis,
    *,
    me_extras: MeExtrasProvider,
    provisioner: ProfileProvisioner,
) -> IdentityServices:
    services = IdentityServices(
        passwords=PasswordService.from_settings(settings),
        tokens=create_token_service(settings),
        denylist=SessionDenylist(redis, ttl_seconds=settings.access_token_ttl_seconds),
        me_extras=me_extras,
        provisioner=provisioner,
    )
    await services.passwords.warm_up()
    return services
