"""Тела запросов социального графа (5.4)."""

import uuid

from pydantic import ConfigDict

from messunjerr.core.schemas import ApiModel


class SendFriendRequestBody(ApiModel):
    """`POST /friend-requests`: кому отправить заявку."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"user_id": "0192b7a0-5c1e-7c3a-9d54-3f1a2b6c7d80"}]},
    )

    user_id: uuid.UUID
