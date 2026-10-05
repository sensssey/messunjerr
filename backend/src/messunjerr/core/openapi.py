"""OpenAPI: описание ошибок problem+json и вспомогательные функции для маршрутов."""

from typing import Any

from fastapi import FastAPI

from messunjerr.core.codes import PROBLEM_SPECS, ErrorCode
from messunjerr.core.problems import PROBLEM_MEDIA_TYPE, Problem

ResponsesDict = dict[int | str, dict[str, Any]]


def problem_responses(*codes: ErrorCode) -> ResponsesDict:
    """Секция `responses` маршрута: коды ошибок, которые он может вернуть, в формате problem+json."""
    result: ResponsesDict = {}
    for code in codes:
        status, title = PROBLEM_SPECS[code]
        entry = result.setdefault(
            status,
            {
                "description": [],
                "content": {
                    PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}
                },
            },
        )
        entry["description"].append(f"`{code.value}`: {title}")
    for entry in result.values():
        entry["description"] = "; ".join(entry["description"])
    return result


def install_openapi(app: FastAPI) -> None:
    """Добавляет в схему компонент `Problem`, на который ссылаются `problem_responses`."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = original()
        components: dict[str, Any] = schema.setdefault("components", {}).setdefault("schemas", {})
        problem = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
        components.update(problem.pop("$defs", {}))
        components["Problem"] = problem
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # pyright: ignore[reportAttributeAccessIssue]
