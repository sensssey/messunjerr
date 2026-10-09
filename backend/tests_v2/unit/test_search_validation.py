"""Поиск людей без базы (S8-04): проверка входа, потолок глубины и согласованность запроса с индексами."""

import re
import unicodedata
import uuid
from typing import Any, cast

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticCustomError
from sqlalchemy import Table, TextClause
from sqlalchemy.dialects.postgresql import asyncpg

from messunjerr.core.codes import ItemCode
from messunjerr.core.errors import ValidationFailedError
from messunjerr.identity.infra.models import UserRow
from messunjerr.profiles.infra.models import ProfileRow
from messunjerr.social.api.search import check_depth, search_text
from messunjerr.social.queries.search import (
    MAX_DEPTH,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    SEARCH_SETTINGS,
    SIMILARITY_THRESHOLD,
    WORD_SIMILARITY_THRESHOLD,
    fold_query,
    is_searchable,
    may_match_login,
    search_statement,
)
from messunjerr.social.queries.search_models import (
    SearchPage,
    UserSearchItem,
    UserSearchPage,
)


# ----------------------------------------------------------------------------- строка поиска
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("анна", "анна"),
        ("  анна  ", "анна"),
        ("ab", "ab"),
        ("я" * 50, "я" * 50),
        ("  " + "я" * 50 + "  ", "я" * 50),
        (unicodedata.normalize("NFD", "йохан"), "йохан"),
        ("Иван Петров", "Иван Петров"),  # регистр не трогает: его снимает сам запрос
    ],
)
def test_the_search_text_is_normalized_before_its_length_is_checked(
    raw: str, expected: str
) -> None:
    assert search_text(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "   ", "а", " а ", "\t\n"])
def test_a_short_search_text_is_string_too_short(raw: str) -> None:
    with pytest.raises(PydanticCustomError) as caught:
        search_text(raw)

    assert caught.value.type == ItemCode.STRING_TOO_SHORT.value
    assert caught.value.context == {"min_length": MIN_QUERY_LENGTH}


@pytest.mark.parametrize("raw", ["я" * 51, "x" * 500, " " + "я" * 51])
def test_a_long_search_text_is_string_too_long(raw: str) -> None:
    with pytest.raises(PydanticCustomError) as caught:
        search_text(raw)

    assert caught.value.type == ItemCode.STRING_TOO_LONG.value
    assert caught.value.context == {"max_length": MAX_QUERY_LENGTH}


@pytest.mark.parametrize("raw", ["\x00", "ab\x00", "\x07ab", "a\x7fb", "ab\x1b"])
def test_control_characters_are_invalid_format_even_when_the_text_is_otherwise_fine(
    raw: str,
) -> None:
    with pytest.raises(PydanticCustomError) as caught:
        search_text(raw)

    assert caught.value.type == ItemCode.INVALID_FORMAT.value


def test_the_bounds_are_the_ones_of_the_specification() -> None:
    assert (MIN_QUERY_LENGTH, MAX_QUERY_LENGTH, MAX_LIMIT, MAX_DEPTH) == (2, 50, 50, 200)
    assert SIMILARITY_THRESHOLD == 0.2


# ----------------------------------------------------------------------------- потолок глубины
def test_the_depth_check_matches_offset_plus_limit_up_to_two_hundred() -> None:
    for offset in range(260):
        for limit in range(1, MAX_LIMIT + 1):
            if offset + limit <= MAX_DEPTH:
                check_depth(offset=offset, limit=limit)
                continue
            with pytest.raises(ValidationFailedError) as caught:
                check_depth(offset=offset, limit=limit)
            (item,) = caught.value.errors
            assert item.code == ItemCode.OUT_OF_RANGE
            if offset >= MAX_DEPTH:
                assert (item.pointer, item.meta) == ("/query/offset", {"max": MAX_DEPTH - 1})
            else:
                assert (item.pointer, item.meta) == ("/query/limit", {"max": MAX_DEPTH - offset})
                assert (
                    offset + item.meta["max"] == MAX_DEPTH
                )  # допустимое значение действительно проходит
                check_depth(offset=offset, limit=item.meta["max"])


# ----------------------------------------------------------------------------- модели ответа
@pytest.mark.parametrize("model", [UserSearchItem, UserSearchPage], ids=lambda m: m.__name__)
def test_the_response_models_have_examples_that_validate_and_show_every_key(
    model: type[BaseModel],
) -> None:
    """То же, что `test_openapi_examples.py` требует от каждой модели ответа (координатор вносит их в MODELS)."""
    examples: list[dict[str, Any]] = model.model_json_schema().get("examples", [])
    assert examples
    for example in examples:
        assert model.model_validate(example)
        assert set(example) == set(model.model_fields)  # ключи формы всегда на месте (5.1)


def test_the_page_is_a_generic_shell_that_later_searches_reuse() -> None:
    assert issubclass(UserSearchPage, SearchPage)
    assert set(SearchPage.model_fields) == {"items", "next_offset"}
    assert UserSearchPage.model_json_schema()["properties"]["items"]["items"] == {
        "$ref": "#/$defs/UserSearchItem"
    }


# ----------------------------------------------------------------------------- вид строки в запросе
@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("ПЁТР Ёлкин", "петр елкин"),
        ("ёЁ", "ее"),
        ("Ivan_Petrov", "ivan_petrov"),
        ("ЙОХАН", "йохан"),  # «й» не равна «и»: на неё правило «ё/е» не распространяется
    ],
)
def test_the_query_is_folded_to_lowercase_with_yo_as_ye(raw: str, folded: str) -> None:
    assert fold_query(raw) == folded


@pytest.mark.parametrize("query", ["--", "!!", "%%", " . ", "()", "\\\\"])
def test_a_query_of_punctuation_alone_is_not_searchable(query: str) -> None:
    assert not is_searchable(fold_query(query))


@pytest.mark.parametrize("query", ["ab", "иван", "__", "12", "ё", "a%", "анна 1"])
def test_anything_with_a_letter_a_digit_or_underscore_is_searchable(query: str) -> None:
    assert is_searchable(fold_query(query))


@pytest.mark.parametrize(
    ("query", "may_match"),
    [("ab", True), ("иван петров", False), ("иван_1", True), ("Zoë", True), ("жук", False)],
)
def test_the_login_branch_is_skipped_when_no_character_can_occur_in_a_login(
    query: str, may_match: bool
) -> None:
    assert may_match_login(fold_query(query)) is may_match


# ----------------------------------------------------------------------------- запрос и индексы
def compiled(query: str) -> str:
    statement = search_statement(uuid.uuid4(), query, limit=21, offset=0)
    return str(statement.compile(dialect=asyncpg.dialect(), compile_kwargs={"literal_binds": True}))


def model_index_text(table: object, name: str) -> str:
    """Текст выражения индекса, как его объявила модель (первое и единственное выражение)."""
    indexes = {str(index.name): index for index in cast(Table, table).indexes}
    return cast(TextClause, indexes[name].expressions[0]).text


def test_the_name_expression_of_the_query_is_the_one_of_the_trigram_index() -> None:
    """Планировщик берёт индекс, только если выражения совпадают буква в букву: запрос и модель держатся
    друг за друга этим тестом, миграция (`test_search_migration`) сверяет определение в самой БД."""
    index_text = model_index_text(ProfileRow.__table__, "ix_profiles_display_name_trgm")
    expression = re.fullmatch(r"\((.+)\) gin_trgm_ops", index_text)
    assert expression is not None
    assert "'ёЁ'" in expression.group(1)
    assert "'еЕ'" in expression.group(1)

    sql = compiled("иван")

    # В запросе колонка с именем таблицы; в индексе без; литералы, а не параметры (общий план).
    assert expression.group(1).replace("display_name", "profile.profiles.display_name") in sql
    assert "$" not in sql
    assert "%(" not in sql


def test_the_login_expression_is_a_text_cast_of_the_lowercase_username() -> None:
    assert model_index_text(UserRow.__table__, "ix_users_username_trgm") == (
        "(username::text) gin_trgm_ops"
    )
    assert model_index_text(UserRow.__table__, "ix_users_username_prefix") == (
        "(username::text) text_pattern_ops"
    )

    sql = compiled("ivan")

    assert "CAST(identity.users.username AS TEXT)" in sql  # username::text, тип индексов
    assert 'COLLATE "C"' in sql  # порядок равных мер побайтовый


def test_a_russian_query_has_no_login_branch_and_a_latin_one_has_both() -> None:
    russian, latin = compiled("иван петров"), compiled("ivan petrov")

    assert "<% CAST(identity.users.username AS TEXT)" not in russian  # ветки ника нет
    assert "<% CAST(identity.users.username AS TEXT)" in latin
    assert "<% translate(profile.profiles.display_name" in russian  # ветка имени есть всегда
    assert "<% translate(profile.profiles.display_name" in latin
    assert russian.count("UNION ALL") == 1  # только блокировки (`hidden`)
    assert latin.count("UNION ALL") == 2  # блокировки и две ветки поиска


def test_the_search_is_one_statement_that_applies_every_filter_and_the_page() -> None:
    sql = compiled("анна")

    assert "status = 'active'" in sql
    assert "identity.users.id !=" in sql  # сам зритель
    assert "NOT (EXISTS" in sql  # блокировки
    assert "LIMIT 21" in sql
    assert "OFFSET 0" in sql
    assert "ORDER BY" in sql


def test_the_transaction_settings_are_what_the_comments_promise() -> None:
    assert SEARCH_SETTINGS["pg_trgm.similarity_threshold"] == str(SIMILARITY_THRESHOLD)
    assert SEARCH_SETTINGS["pg_trgm.word_similarity_threshold"] == str(WORD_SIMILARITY_THRESHOLD)
    assert WORD_SIMILARITY_THRESHOLD == 0.6
    assert SEARCH_SETTINGS["plan_cache_mode"] == "force_custom_plan"
    assert float(SEARCH_SETTINGS["cpu_operator_cost"]) > 0.0025  # дороже умолчания PostgreSQL
