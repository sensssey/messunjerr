"""Службы контекста identity, которые создаются один раз при старте процесса."""

from dataclasses import dataclass

from messunjerr.identity.infra.jwt_service import TokenService, create_token_service
from messunjerr.identity.infra.password_service import PasswordService
from messunjerr.settings import Settings


@dataclass(slots=True)
class IdentityServices:
    passwords: PasswordService
    tokens: TokenService

    def close(self) -> None:
        self.passwords.shutdown()


async def create_identity_services(settings: Settings) -> IdentityServices:
    services = IdentityServices(
        passwords=PasswordService.from_settings(settings),
        tokens=create_token_service(settings),
    )
    await services.passwords.warm_up()
    return services
