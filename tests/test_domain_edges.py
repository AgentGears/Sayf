from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import LedgerReadError, SQLiteEventStore


def test_payload_rejects_unpaired_unicode_surrogate() -> None:
    with pytest.raises(ValidationError):
        EventDraft(
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
            payload={"value": "\ud800"},
        )


def test_json_object_key_rejects_unpaired_unicode_surrogate() -> None:
    with pytest.raises(ValidationError):
        EventDraft(
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
            payload={"\ud800": "value"},
        )


def test_hashed_identifier_rejects_unpaired_unicode_surrogate() -> None:
    with pytest.raises(ValidationError):
        EventDraft(
            stream_id="project:\ud800",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        )


def test_extreme_aware_timestamp_becomes_validation_error() -> None:
    plus_fourteen = timezone(timedelta(hours=14))

    with pytest.raises(ValidationError, match="cannot be normalized to UTC"):
        EventDraft(
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
            occurred_at=datetime(1, 1, 1, 0, 0, tzinfo=plus_fourteen),
        )


def test_mutated_surrogate_is_rejected_before_database_creation(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    draft = EventDraft(
        stream_id="project:test",
        event_type="RecordCreated",
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        payload={"value": "valid"},
    )
    draft.payload["value"] = "\ud800"

    with pytest.raises(LedgerReadError, match="event draft is not canonical JSON"):
        SQLiteEventStore(path).append(draft)

    assert path.exists() is False
