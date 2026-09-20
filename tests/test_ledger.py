from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.hashing import canonical_json, compute_event_hash
from sayf.ids import uuid7
from sayf.storage import LedgerReadError, SQLiteEventStore


def _draft(event_type: str, payload: dict[str, object] | None = None) -> EventDraft:
    return EventDraft(
        stream_id="project:test",
        event_type=event_type,
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        payload=payload or {},
    )


def _restore_update_trigger(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TRIGGER events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
            END;
            """
        )


def test_uuid7_is_rfc_variant_and_version() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_event_draft_rejects_timezone_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        EventDraft(
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
            occurred_at=datetime(2026, 9, 19, 12, 0, 0),
        )


def test_event_draft_normalizes_aware_timestamp_to_utc() -> None:
    riyadh = timezone(timedelta(hours=3))
    draft = EventDraft(
        stream_id="project:test",
        event_type="RecordCreated",
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        occurred_at=datetime(2026, 9, 19, 12, 0, 0, tzinfo=riyadh),
    )

    assert draft.occurred_at == datetime(2026, 9, 19, 9, 0, 0, tzinfo=UTC)
    assert draft.occurred_at.tzinfo is UTC


def test_event_draft_rejects_empty_event_id() -> None:
    with pytest.raises(ValidationError):
        EventDraft(
            event_id="",
            stream_id="project:test",
            event_type="RecordCreated",
            actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        )


def test_event_payload_rejects_non_json_and_non_finite_values() -> None:
    with pytest.raises(ValidationError, match="unsupported JSON value type"):
        _draft("RecordCreated", {"when": datetime.now(UTC)})

    with pytest.raises(ValidationError, match="JSON numbers must be finite"):
        _draft("RecordCreated", {"score": float("nan")})


def test_actor_metadata_rejects_non_json_values() -> None:
    with pytest.raises(ValidationError, match="unsupported JSON value type"):
        Actor(kind=ActorKind.HUMAN, id="tester", metadata={"roles": {"admin"}})


def test_json_objects_are_detached_from_caller_input() -> None:
    payload = {"items": ["one"]}
    metadata = {"roles": ["reviewer"]}
    actor = Actor(kind=ActorKind.HUMAN, id="tester", metadata=metadata)
    draft = EventDraft(
        stream_id="project:test",
        event_type="RecordCreated",
        actor=actor,
        payload=payload,
    )

    payload["items"].append("two")
    metadata["roles"].append("admin")

    assert draft.payload == {"items": ["one"]}
    assert draft.actor.metadata == {"roles": ["reviewer"]}


def test_append_revalidates_mutated_draft_before_creating_database(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    draft = _draft("RecordCreated", {"score": 1.0})
    draft.payload["score"] = float("nan")

    with pytest.raises(LedgerReadError, match="event draft is not canonical JSON"):
        SQLiteEventStore(path).append(draft)

    assert path.exists() is False


def test_verify_missing_database_fails_without_creating_it(tmp_path) -> None:
    path = tmp_path / "missing-ledger.sqlite3"
    result = SQLiteEventStore(path).verify()

    assert result.valid is False
    assert result.checked_events == 0
    assert result.reason == "ledger database does not exist"
    assert path.exists() is False


def test_events_missing_database_fails_without_creating_it(tmp_path) -> None:
    path = tmp_path / "missing-ledger.sqlite3"

    with pytest.raises(LedgerReadError, match="ledger database does not exist"):
        SQLiteEventStore(path).events()

    assert path.exists() is False


def test_uninitialized_database_is_not_mutated_by_read_or_init(tmp_path) -> None:
    path = tmp_path / "not-a-ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()

    result = SQLiteEventStore(path).verify()
    assert result.valid is False
    assert result.reason == "Sayf ledger schema marker is missing"

    with pytest.raises(LedgerReadError, match="schema marker is missing"):
        SQLiteEventStore(path).events()
    with pytest.raises(LedgerReadError, match="schema marker is missing"):
        SQLiteEventStore(path).initialize()

    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert names == {"unrelated"}


def test_initialize_is_idempotent_only_for_valid_ledger(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()
    store.initialize()

    assert store.verify().valid is True


def test_initialize_refuses_to_repair_damaged_schema(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_delete")
        connection.commit()

    with pytest.raises(LedgerReadError, match="schema objects are unexpected or incomplete"):
        store.initialize()

    with sqlite3.connect(path) as connection:
        trigger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'events_no_delete'"
        ).fetchone()
    assert trigger is None


def test_verify_rejects_unsupported_schema_version(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sayf_ledger_meta SET value = '999' WHERE key = 'schema_version'"
        )
        connection.commit()

    result = store.verify()
    assert result.valid is False
    assert result.reason == "Sayf ledger schema version is unsupported"


def test_append_rejects_unsupported_schema_without_writing(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sayf_ledger_meta SET value = '999' WHERE key = 'schema_version'"
        )
        connection.commit()

    with pytest.raises(LedgerReadError, match="schema version is unsupported"):
        store.append(_draft("RecordCreated"))

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        version = connection.execute(
            "SELECT value FROM sayf_ledger_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert count == 0
    assert version == "999"


def test_append_non_sqlite_file_returns_ledger_error_without_rewriting(tmp_path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    original = "this is not sqlite"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(LedgerReadError, match="unable to append to ledger"):
        SQLiteEventStore(path).append(_draft("RecordCreated"))

    assert path.read_text(encoding="utf-8") == original


def test_schema_validation_rejects_unexpected_trigger_before_append(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TRIGGER swallow_insert
            BEFORE INSERT ON events
            BEGIN
                SELECT RAISE(IGNORE);
            END;
            """
        )

    result = store.verify()
    assert result.valid is False
    assert result.reason == "Sayf ledger schema objects are unexpected or incomplete"

    with pytest.raises(LedgerReadError, match="schema objects are unexpected or incomplete"):
        store.append(_draft("RecordCreated"))

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 0


def test_verify_rejects_lookalike_schema_with_partial_unique_indexes(tmp_path) -> None:
    path = tmp_path / "lookalike.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sayf_ledger_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO sayf_ledger_meta (key, value) VALUES ('schema_version', '1');
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL
            );
            CREATE UNIQUE INDEX fake_event_id_unique ON events(event_id) WHERE 0;
            CREATE UNIQUE INDEX fake_event_hash_unique ON events(event_hash) WHERE 0;
            CREATE INDEX idx_events_stream_sequence ON events(stream_id, sequence);
            CREATE TRIGGER events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
            END;
            CREATE TRIGGER events_no_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
            END;
            """
        )

    result = SQLiteEventStore(path).verify()
    assert result.valid is False
    assert result.reason == "Sayf ledger schema objects are unexpected or incomplete"


def test_verify_explicitly_initialized_empty_ledger_is_valid(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    result = store.verify()
    assert result.valid is True
    assert result.checked_events == 0
    assert result.failure_sequence is None
    assert result.reason is None


def test_append_builds_global_hash_chain_and_round_trips_json(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "ledger.sqlite3")
    first = store.append(_draft("RecordCreated", {"record_id": "r1", "flags": [True, None]}))
    second = store.append(_draft("RecordCreated", {"record_id": "r2"}))

    assert first.sequence == 1
    assert first.previous_event_hash is None
    assert second.sequence == 2
    assert second.previous_event_hash == first.event_hash
    assert store.events()[0].payload == first.payload
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


def test_verify_detects_unrecomputed_rewrite_after_storage_guard_bypass(tmp_path) -> None:
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
    _restore_update_trigger(path)

    result = store.verify()
    assert result.valid is False
    assert result.failure_sequence == 1
    assert result.reason == "event hash does not match canonical event content"


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        ("actor_json", "{}"),
        ("payload_json", "{"),
        ("occurred_at", "not-a-timestamp"),
        ("occurred_at", "0001-01-01T00:00:00+14:00"),
        ("event_type", ""),
    ],
)
def test_verify_reports_malformed_rows_as_verification_failures(
    tmp_path,
    column: str,
    corrupt_value: str,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", {"record_id": "r1"}))
    store.append(_draft("RecordCreated", {"record_id": "r2"}))

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(
            f"UPDATE events SET {column} = ? WHERE sequence = 2",
            (corrupt_value,),
        )
        connection.commit()
    _restore_update_trigger(path)

    result = store.verify()
    assert result.valid is False
    assert result.checked_events == 1
    assert result.failure_sequence == 2
    assert result.reason is not None
    assert result.reason.startswith("malformed event row:")


def test_verify_reports_deeply_nested_json_as_verification_failure(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", {"record_id": "r1"}))

    nested_json = "[" * 2000 + "0" + "]" * 2000
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE sequence = 1",
            (nested_json,),
        )
        connection.commit()
    _restore_update_trigger(path)

    result = store.verify()
    assert result.valid is False
    assert result.checked_events == 0
    assert result.failure_sequence == 1
    assert result.reason is not None
    assert result.reason.startswith("malformed event row:")


def test_verify_rejects_non_contiguous_sequence_even_with_valid_hash(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()
    draft = _draft("RecordCreated", {"record_id": "r2"})
    event_hash = compute_event_hash(draft, sequence=2, previous_event_hash=None)

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO events (
                sequence, event_id, stream_id, event_type, occurred_at,
                actor_json, payload_json, previous_event_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                draft.event_id,
                draft.stream_id,
                draft.event_type,
                draft.occurred_at.isoformat(),
                canonical_json(draft.actor.model_dump(mode="json")),
                canonical_json(draft.payload),
                None,
                event_hash,
            ),
        )
        connection.commit()

    result = store.verify()
    assert result.valid is False
    assert result.failure_sequence == 2
    assert result.reason == "event sequence is not contiguous: expected 1, found 2"


def test_verify_streams_event_rows_without_fetchall(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", {"record_id": "r1"}))
    store.append(_draft("RecordCreated", {"record_id": "r2"}))

    class CursorProxy:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self.cursor = cursor

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            raise AssertionError("verification must stream event rows")

    class ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, sql: str):
            cursor = self.connection.execute(sql)
            if sql.startswith("SELECT * FROM events ORDER BY sequence"):
                return CursorProxy(cursor)
            return cursor

    @contextmanager
    def streaming_read_connection():
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            yield ConnectionProxy(connection)

    monkeypatch.setattr(store, "_read_connection", streaming_read_connection)

    result = store.verify()
    assert result.valid is True
    assert result.checked_events == 2


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

    with pytest.raises(LedgerReadError, match="UNIQUE constraint failed: events.event_id"):
        store.append(duplicate)
