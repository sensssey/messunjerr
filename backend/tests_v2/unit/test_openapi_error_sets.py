"""Общие наборы ошибок для OpenAPI: один источник для роутеров identity и profiles (5.1)."""

from messunjerr.core.codes import ErrorCode
from messunjerr.core.openapi import LIMIT_ERRORS, TOKEN_ERRORS, problem_responses


def test_token_errors_are_documented_as_401_with_every_code() -> None:
    assert set(TOKEN_ERRORS) == {401}
    description = TOKEN_ERRORS[401]["description"]
    for code in ("token_missing", "token_invalid", "token_expired", "session_revoked"):
        assert f"`{code}`" in description


def test_limit_errors_cover_rate_limits_and_unavailable_dependencies() -> None:
    assert set(LIMIT_ERRORS) == {429, 503}
    assert "`rate_limited`" in LIMIT_ERRORS[429]["description"]
    assert "`service_unavailable`" in LIMIT_ERRORS[503]["description"]


def test_responses_point_at_the_problem_schema_in_problem_json() -> None:
    entry = TOKEN_ERRORS[401]["content"]["application/problem+json"]
    assert entry["schema"] == {"$ref": "#/components/schemas/Problem"}


def test_the_error_sets_merge_without_losing_statuses() -> None:
    merged = {
        **TOKEN_ERRORS,
        **problem_responses(ErrorCode.ACCOUNT_DELETION_PENDING),
        **LIMIT_ERRORS,
    }
    assert set(merged) == {401, 403, 429, 503}
