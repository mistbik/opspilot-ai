import json
from typing import Any

from pydantic import BaseModel, Field, field_validator


class WebhookEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any]

    @field_validator("event_id", "event_type")
    @classmethod
    def validate_database_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Text cannot contain NUL characters")
        value.encode("utf-8")
        return value

    @field_validator("payload")
    @classmethod
    def validate_database_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, RecursionError) as error:
            raise ValueError("Payload must contain valid JSON values") from error
        return value
