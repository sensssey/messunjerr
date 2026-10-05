"""Cookie с refresh-токеном (4.7): `HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth`."""

from fastapi import Response

REFRESH_COOKIE = "__Secure-mj_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def set_refresh_cookie(response: Response, token: str, *, max_age: int) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    """Стирает cookie: те же путь и атрибуты, `Max-Age=0`."""
    response.delete_cookie(
        key=REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def refresh_cookie_clearing_header() -> str:
    """Значение `Set-Cookie`, стирающее cookie, для ответов-ошибок (их собирает обработчик исключений)."""
    probe = Response()
    clear_refresh_cookie(probe)
    return probe.headers["set-cookie"]
