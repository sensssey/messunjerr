"""Зависимости FastAPI контекста profiles."""

from typing import Annotated, cast

from fastapi import Depends, Request

from messunjerr.profiles.services import ProfileServices


def get_profiles(request: Request) -> ProfileServices:
    return cast(ProfileServices, request.app.state.profiles)  # pyright: ignore[reportUnknownMemberType]


ProfilesDep = Annotated[ProfileServices, Depends(get_profiles)]
