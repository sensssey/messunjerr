"""Зависимости FastAPI контекста media."""

from typing import Annotated, cast

from fastapi import Depends, Request

from messunjerr.media.services import MediaServices


def get_media(request: Request) -> MediaServices:
    return cast(MediaServices, request.app.state.media)  # pyright: ignore[reportUnknownMemberType]


MediaDep = Annotated[MediaServices, Depends(get_media)]
