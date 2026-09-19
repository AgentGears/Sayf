from __future__ import annotations

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


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ActorKind
    id: str = Field(min_length=1)
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


class EventDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stream_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    actor: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_id: str = Field(default_factory=lambda: new_id("evt"))

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class LedgerEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    sequence: int = Field(ge=1)
    stream_id: str
    event_type: str
    occurred_at: datetime
    actor: Actor
    payload: dict[str, Any]
    previous_event_hash: str | None
    event_hash: str

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class LedgerVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    checked_events: int
    failure_sequence: int | None = None
    reason: str | None = None
