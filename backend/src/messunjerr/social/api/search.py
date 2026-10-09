"""Маршрут поиска людей `GET /search/users` (5.7, S8-04).

Права и фильтры живут в запросе (`social.queries.search`): аккаунты не `active`, блокировки и сам
зритель в выдачу не попадают. Ручка проверяет только входные данные: строку поиска (после
нормализации 2–50 символов), `limit` и потолок глубины.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

from messunjerr.core.codes import ErrorCode, ItemCode
from messunjerr.core.deps import UowDep
from messunjerr.core.errors import ErrorItem, ValidationFailedError
from messunjerr.core.openapi import LIMIT_ERRORS, TOKEN_ERRORS, problem_responses
from messunjerr.core.schemas import normalize_text
from messunjerr.identity.api_public import PrincipalDep, limit_user, no_store
from messunjerr.social.queries.search import (
    DEFAULT_LIMIT,
    MAX_DEPTH,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    search_users,
)
from messunjerr.social.queries.search_models import UserSearchPage

router = APIRouter(prefix="/search", tags=["search"], dependencies=[Depends(no_store)])

_AUTH_ERRORS = {**TOKEN_ERRORS, **problem_responses(ErrorCode.ACCOUNT_DELETION_PENDING)}


def search_text(value: str) -> str:
    """Строка поиска: NFC, края обрезаны, управляющие символы (в том числе NUL, который PostgreSQL в
    тексте не принимает) дают `invalid_format`; длина считается ПОСЛЕ нормализации, поэтому пробелы
    по краям её не увеличивают, а «   » это `string_too_short`."""
    text = normalize_text(value)
    if len(text) < MIN_QUERY_LENGTH:
        raise PydanticCustomError(
            ItemCode.STRING_TOO_SHORT.value,
            "String should have at least {min_length} characters",
            {"min_length": MIN_QUERY_LENGTH},
        )
    if len(text) > MAX_QUERY_LENGTH:
        raise PydanticCustomError(
            ItemCode.STRING_TOO_LONG.value,
            "String should have at most {max_length} characters",
            {"max_length": MAX_QUERY_LENGTH},
        )
    return text


SearchQuery = Annotated[
    str,
    Query(
        description=(
            f"Что искать: ник или имя, от {MIN_QUERY_LENGTH} до {MAX_QUERY_LENGTH} символов после "
            "обрезки пробелов по краям."
        ),
        json_schema_extra={"minLength": MIN_QUERY_LENGTH, "maxLength": MAX_QUERY_LENGTH},
    ),
    AfterValidator(search_text),
]
SearchLimit = Annotated[
    int, Query(ge=1, le=MAX_LIMIT, description=f"Размер страницы, не больше {MAX_LIMIT}.")
]
SearchOffset = Annotated[
    int, Query(ge=0, description="Сколько результатов пропустить (смещение страницы).")
]


def check_depth(*, offset: int, limit: int) -> None:
    """Глубокого листания нет: `offset + limit ≤ 200` (5.7), иначе `422 validation_error` с
    `out_of_range`. Элемент указывает на то, что нужно уменьшить: `offset`, если он сам не меньше
    потолка, иначе `limit`; в `meta.max` самое большое допустимое значение."""
    if offset + limit <= MAX_DEPTH:
        return
    pointer, allowed = (
        ("/query/offset", MAX_DEPTH - 1)
        if offset >= MAX_DEPTH
        else ("/query/limit", MAX_DEPTH - offset)
    )
    raise ValidationFailedError(
        [
            ErrorItem(
                pointer=pointer,
                code=ItemCode.OUT_OF_RANGE,
                detail=f"offset + limit must not exceed {MAX_DEPTH}.",
                meta={"max": allowed},
            )
        ]
    )


@router.get(
    "/users",
    response_model=UserSearchPage,
    summary="Найти людей",
    description=(
        "Поиск по нику и отображаемому имени. Порядок: точное совпадение ника, префикс ника, затем "
        "сходство (`pg_trgm`) по нику и имени; внутри группы по убыванию сходства, затем по нику. "
        "Регистр не важен, «ё» и «е» одна буква, слова имени можно называть в любом порядке (`иван "
        "петров` находит `Петров Иван`), начало слова и небольшая опечатка находят человека. Не "
        "находятся: вы сами, аккаунты не `active`, заблокировавшие вас и заблокированные вами. "
        "Видны только ник, имя и аватар (категория `basic`); `relationship` это отношение к вам. "
        "Страницы по смещению: `next_offset` или `null`; глубже 200 результатов не листается "
        "(`offset + limit ≤ 200`, иначе `422`; следующую страницу просите с `limit ≤ 200 − "
        "next_offset`). Лимиты `search` (30 в минуту) и `api_read`."
    ),
    dependencies=[Depends(limit_user("search", "api_read"))],
    responses={
        **_AUTH_ERRORS,
        **problem_responses(ErrorCode.VALIDATION_ERROR),
        **LIMIT_ERRORS,
    },
)
async def search_users_endpoint(
    principal: PrincipalDep,
    uow: UowDep,
    q: SearchQuery,
    limit: SearchLimit = DEFAULT_LIMIT,
    offset: SearchOffset = 0,
) -> UserSearchPage:
    check_depth(offset=offset, limit=limit)
    return await search_users(
        uow.session, viewer_id=principal.user_id, q=q, limit=limit, offset=offset
    )
