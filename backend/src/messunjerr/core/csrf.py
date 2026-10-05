"""Защита cookie-ручек от CSRF (4.7): `POST /auth/refresh` и `POST /auth/logout`.

Остальные ручки принимают `Authorization: Bearer`, а не cookie, и CSRF им не грозит. Для cookie-ручек
к `SameSite=Strict` добавляются три проверки (при несовпадении `403 csrf_failed`):

1. заголовок `X-Requested-With: messunjerr`: чужая страница не может его отправить без
   предварительного CORS-запроса, а мы его не разрешаем;
2. `Origin`, если он есть: только адрес клиента и `ALLOWED_ORIGINS`;
3. `Sec-Fetch-Site`, если он есть: `same-origin` (и `none` для прямых переходов); в разработке ещё
   `same-site`, потому что клиент на :3000 и API на :8000 это один сайт, но разные источники.
"""

from fastapi import Request

from messunjerr.core.codes import ErrorCode
from messunjerr.core.deps import ResourcesDep
from messunjerr.core.errors import DomainError

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "messunjerr"


def _csrf_failed(reason: str) -> DomainError:
    return DomainError(ErrorCode.CSRF_FAILED, reason)


async def require_csrf_guard(request: Request, resources: ResourcesDep) -> None:
    """Зависимость cookie-ручек."""
    settings = resources.settings
    headers = request.headers
    if headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise _csrf_failed(f"The {CSRF_HEADER}: {CSRF_VALUE} header is required.")

    origin = headers.get("origin")
    if origin is not None and origin.rstrip("/") not in settings.origins:
        raise _csrf_failed("The request origin is not allowed.")

    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        allowed = {"same-origin", "none"}
        if settings.app_env != "prod":
            allowed.add("same-site")
        if fetch_site.lower() not in allowed:
            raise _csrf_failed("The request comes from another site.")
