from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sayf.ids import new_id


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    TOOL = "tool"
    POLICY_ENGINE = "policy_engine"
    RUNTIME_ADAPTER = "runtime_adapter"
    SYSTEM = "system"


def _normalize_text(value: str, field_name: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    return str(value)


def _normalize_json_value(value: Any) -> Any:
    """Return a detached JSON-native value or reject unsupported input."""
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return _normalize_text(value, "JSON string")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return float(value)
    if isinstance(value, list):
        try:
            return [_normalize_json_value(item) for item in value]
        except RecursionError as exc:
            raise ValueError("JSON value nesting is too deep") from exc
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        try:
            return {
                _normalize_text(key, "JSON object key"): _normalize_json_value(item)
                for key, item in value.items()
            }
        except RecursionError as exc:
            raise ValueError("JSON value nesting is too deep") from exc
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


def _normalize_json_object(value: Any, field_name: str) -> dict[str, Any]:
    normalized = _normalize_json_value(value)
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return normalized


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    kind: ActorKind
    id: str = Field(min_length=1)
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return _normalize_text(value, "actor id")

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "actor display_name")

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "metadata")


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


class EventDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    stream_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    actor: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_id: str = Field(default_factory=lambda: new_id("evt"), min_length=1)

    @field_validator("stream_id", "event_type", "event_id")
    @classmethod
    def normalize_identifiers(cls, value: str) -> str:
        return _normalize_text(value, "event identifier")

    @field_validator("payload", mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "payload")

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class LedgerEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    stream_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: datetime
    actor: Actor
    payload: dict[str, Any]
    previous_event_hash: str | None
    event_hash: str = Field(min_length=1)

    @field_validator("event_id", "stream_id", "event_type", "event_hash")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_text(value, "ledger event text")

    @field_validator("previous_event_hash")
    @classmethod
    def normalize_previous_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "previous event hash")

    @field_validator("payload", mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "payload")

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class LedgerVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    checked_events: int = Field(ge=0)
    failure_sequence: int | None = Field(default=None, ge=1)
    reason: str | None = None
