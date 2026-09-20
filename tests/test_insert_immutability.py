from __future__ import annotations

import sqlite3

import pytest

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import SQLiteEventStore


def _draft() -> EventDraft:
    return EventDraft(
        stream_id="project:test",
        event_type="RecordCreated",
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        payload={"record_id": "r1"},
    )


@pytest.mark.parametrize("conflict_column", ["sequence", "event_id", "event_hash"])
def test_insert_or_replace_cannot_rewrite_existing_event(
    tmp_path,
    conflict_column: str,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    original = store.append(_draft())

    replacement = {
        "sequence": original.sequence + 100,
        "event_id": "evt_replacement",
        "stream_id": "project:replacement",
        "event_type": "Tampered",
        "occurred_at": original.occurred_at.isoformat(),
        "actor_json": '{"display_name":null,"id":"attacker","kind":"human","metadata":{}}',
        "payload_json": '{"record_id":"rewritten"}',
        "previous_event_hash": None,
        "event_hash": "sha256:replacement",
    }
    replacement[conflict_column] = getattr(original, conflict_column)

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                INSERT OR REPLACE INTO events (
                    sequence, event_id, stream_id, event_type, occurred_at,
                    actor_json, payload_json, previous_event_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    replacement["sequence"],
                    replacement["event_id"],
                    replacement["stream_id"],
                    replacement["event_type"],
                    replacement["occurred_at"],
                    replacement["actor_json"],
                    replacement["payload_json"],
                    replacement["previous_event_hash"],
                    replacement["event_hash"],
                ),
            )

    events = store.events()
    assert len(events) == 1
    assert events[0] == original
    assert store.verify().valid is True
