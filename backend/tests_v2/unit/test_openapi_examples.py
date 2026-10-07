"""Примеры в OpenAPI (определение готовности эндпоинта, 2.2) не должны расходиться с самими моделями."""

from typing import Any

import pytest
from pydantic import BaseModel

from messunjerr.core.me import MeProfile, PrivacySettings
from messunjerr.identity.api.schemas import (
    ChangeUsernameRequest,
    DeleteAccountRequest,
    DeletionScheduledResponse,
    UsernameResponse,
)
from messunjerr.identity.queries.models import MeUser
from messunjerr.media.api.schemas import (
    CompleteUploadResponse,
    InitUploadRequest,
    InitUploadResponse,
)
from messunjerr.media.queries.models import Asset, Quota
from messunjerr.profiles.api.schemas import UpdatePrivacyRequest, UpdateProfileRequest
from messunjerr.profiles.queries.models import UserProfile

MODELS: list[type[BaseModel]] = [
    MeUser,
    MeProfile,
    PrivacySettings,
    UserProfile,
    UpdateProfileRequest,
    UpdatePrivacyRequest,
    ChangeUsernameRequest,
    DeleteAccountRequest,
    UsernameResponse,
    DeletionScheduledResponse,
    InitUploadRequest,
    InitUploadResponse,
    CompleteUploadResponse,
    Asset,
    Quota,
]


def examples_of(model: type[BaseModel]) -> list[dict[str, Any]]:
    schema = model.model_json_schema()
    examples: list[dict[str, Any]] = schema.get("examples", [])
    return examples


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_every_model_of_the_documented_endpoints_has_an_example(model: type[BaseModel]) -> None:
    assert examples_of(model), f"у {model.__name__} нет примера для OpenAPI"


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_examples_pass_validation_by_their_own_model(model: type[BaseModel]) -> None:
    for example in examples_of(model):
        assert model.model_validate(example)


@pytest.mark.parametrize("model", [MeUser, MeProfile, UserProfile], ids=lambda m: m.__name__)
def test_response_examples_show_every_key_of_the_stable_shape(model: type[BaseModel]) -> None:
    """Ответ всегда содержит все ключи (5.1); пример без `null`, поэтому ключей в нём столько же, сколько полей."""
    for example in examples_of(model):
        assert set(example) == set(model.model_fields)
