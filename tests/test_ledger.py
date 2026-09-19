from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.ids import uuid7
from sayf.storage import SQLiteEventStore


def _draft(event_type: str, payload: dict[str, object] | None = None) -> EventDraft:
    return EventDraft(
        stream_id="project:test",
        event_type=event_type,
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        payload=payload or {},
    )


def test_uuid7_is_rfc_variant_and_version() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_append_builds_global_hash_chain(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "ledger.sqlite3")

    first = store.append(_draft("RecordCreated", {"record_id": "r1"}))
    second = store.append(_draft("RecordCreated", {"record_id": "r2"}))

    assert first.sequence == 1
    assert first.previous_event_hash is None
    assert second.sequence == 2
    assert second.previous_event_hash == first.event_hash
    assert store.verify().valid is True


def test_database_rejects_update_and_delete(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated"))

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE events SET event_type = 'Tampered' WHERE sequence = 1")

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM events WHERE sequence = 1")


def test_verify_detects_tampering_even_if_storage_guard_is_bypassed(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", {"record_id": "r1"}))
    store.append(_draft("RelationCreated", {"source": "r1", "target": "r2"}))

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE sequence = 1",
            (json.dumps({"record_id": "rewritten"}),),
        )
        connection.commit()

    result = store.verify()
    assert result.valid is False
    assert result.failure_sequence == 1
    assert result.reason == "event hash does not match canonical event content"


def test_duplicate_event_id_is_rejected(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "ledger.sqlite3")
    first = _draft("RecordCreated")
    store.append(first)

    duplicate = EventDraft(
        event_id=first.event_id,
        stream_id=first.stream_id,
        event_type="AnotherEvent",
        actor=first.actor,
        payload={},
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.append(duplicate)
