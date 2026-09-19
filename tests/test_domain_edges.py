from __future__ import annotations

import pytest
from pydantic import ValidationError

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import LedgerReadError, SQLiteEventStore


def test_payload_rejects_unpaired_unicode_surrogate() -> None:
    with pytest.raises(ValidationError, match="valid UTF-8 text"):
        EventDraft(
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
            payload={"value": "\ud800"},
        )


def test_json_object_key_rejects_unpaired_unicode_surrogate() -> None:
    with pytest.raises(ValidationError, match="valid UTF-8 text"):
        EventDraft(
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
            payload={"\ud800": "value"},
        )


def test_hashed_identifier_rejects_unpaired_unicode_surrogate() -> None:
    with pytest.raises(ValidationError, match="valid UTF-8 text"):
        EventDraft(
            stream_id="project:\ud800",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
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
