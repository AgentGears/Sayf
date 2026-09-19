from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.ids import uuid7
from sayf.storage import LedgerReadError, SQLiteEventStore


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


def test_verify_missing_database_fails_without_creating_it(tmp_path) -> None:
    path = tmp_path / "missing-ledger.sqlite3"
    store = SQLiteEventStore(path)

    result = store.verify()

    assert result.valid is False
    assert result.checked_events == 0
    assert result.reason == "ledger database does not exist"
    assert path.exists() is False


def test_events_missing_database_fails_without_creating_it(tmp_path) -> None:
    path = tmp_path / "missing-ledger.sqlite3"
    store = SQLiteEventStore(path)

    with pytest.raises(LedgerReadError, match="ledger database does not exist"):
        store.events()

    assert path.exists() is False


def test_verify_uninitialized_database_fails_without_mutating_schema(tmp_path) -> None:
    path = tmp_path / "not-a-ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()

    result = SQLiteEventStore(path).verify()

    assert result.valid is False
    assert result.checked_events == 0
    assert result.reason == "Sayf ledger schema marker is missing"
    with sqlite3.connect(path) as connection:
        events_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    assert events_table is None


def test_events_uninitialized_database_fails_without_mutating_schema(tmp_path) -> None:
    path = tmp_path / "not-a-ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()

    with pytest.raises(LedgerReadError, match="Sayf ledger schema marker is missing"):
        SQLiteEventStore(path).events()

    with sqlite3.connect(path) as connection:
        events_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    assert events_table is None


def test_verify_lookalike_events_table_without_sayf_marker_fails(tmp_path) -> None:
    path = tmp_path / "lookalike.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                stream_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.commit()

    result = SQLiteEventStore(path).verify()

    assert result.valid is False
    assert result.reason == "Sayf ledger schema marker is missing"


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


def test_verify_rejects_missing_immutability_trigger(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_delete")
        connection.commit()

    result = store.verify()

    assert result.valid is False
    assert result.reason == "Sayf ledger trigger events_no_delete is missing or malformed"


def test_verify_rejects_disabled_immutability_trigger(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.executescript(
            """
            CREATE TRIGGER events_no_update
            BEFORE UPDATE ON events
            WHEN 0
            BEGIN
                SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
            END;
            """
        )

    result = store.verify()

    assert result.valid is False
    assert result.reason == "Sayf ledger trigger events_no_update is missing or malformed"


def test_verify_rejects_missing_unique_constraints(tmp_path) -> None:
    path = tmp_path / "fake-ledger.sqlite3"
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
    assert result.reason == "Sayf ledger unique constraints are incomplete"


def test_verify_explicitly_initialized_empty_ledger_is_valid(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    result = store.verify()

    assert result.valid is True
    assert result.checked_events == 0
    assert result.failure_sequence is None
    assert result.reason is None


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
    store.initialize()

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
    store.initialize()

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
    store.initialize()

    result = store.verify()
    assert result.valid is False
    assert result.checked_events == 0
    assert result.failure_sequence == 1
    assert result.reason is not None
    assert result.reason.startswith("malformed event row:")


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

    with pytest.raises(sqlite3.IntegrityError):
        store.append(duplicate)
