from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from sayf.domain import Actor, EventDraft, LedgerEvent, LedgerVerification
from sayf.hashing import canonical_json, compute_event_hash

_LEDGER_SCHEMA_VERSION = "1"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sayf_ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO sayf_ledger_meta (key, value)
VALUES ('schema_version', '{_LEDGER_SCHEMA_VERSION}');

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    stream_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_event_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_events_stream_sequence
    ON events(stream_id, sequence);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
END;
"""

_EXPECTED_EVENT_COLUMNS = {
    "sequence": ("INTEGER", False, True),
    "event_id": ("TEXT", True, False),
    "stream_id": ("TEXT", True, False),
    "event_type": ("TEXT", True, False),
    "occurred_at": ("TEXT", True, False),
    "actor_json": ("TEXT", True, False),
    "payload_json": ("TEXT", True, False),
    "previous_event_hash": ("TEXT", False, False),
    "event_hash": ("TEXT", True, False),
}

_EXPECTED_UNIQUE_COLUMN_SETS = {frozenset({"event_id"}), frozenset({"event_hash"})}
_EXPECTED_IMMUTABILITY_TRIGGERS = {
    "events_no_update": "before update on events",
    "events_no_delete": "before delete on events",
}

_MALFORMED_ROW_ERRORS = (
    json.JSONDecodeError,
    ValidationError,
    TypeError,
    ValueError,
    OverflowError,
    RecursionError,
)


class LedgerReadError(RuntimeError):
    """Raised when an existing Sayf ledger cannot be inspected safely."""


class SQLiteEventStore:
    """Append-only SQLite store for the authoritative Sayf event ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only:
            database = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(
                database,
                timeout=30.0,
                isolation_level=None,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            raise LedgerReadError("ledger database does not exist")
        if not self.path.is_file():
            raise LedgerReadError("ledger database path is not a file")

        try:
            with self._connect(read_only=True) as connection:
                self._validate_schema(connection)
                yield connection
        except LedgerReadError:
            raise
        except sqlite3.Error as exc:
            raise LedgerReadError(f"unable to read ledger database: {exc}") from exc

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def append(self, draft: EventDraft) -> LedgerEvent:
        self.initialize()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT sequence, event_hash FROM events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = 1 if row is None else int(row["sequence"]) + 1
                previous_hash = None if row is None else str(row["event_hash"])
                event_hash = compute_event_hash(
                    draft,
                    sequence=sequence,
                    previous_event_hash=previous_hash,
                )

                connection.execute(
                    """
                    INSERT INTO events (
                        sequence, event_id, stream_id, event_type, occurred_at,
                        actor_json, payload_json, previous_event_hash, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sequence,
                        draft.event_id,
                        draft.stream_id,
                        draft.event_type,
                        draft.occurred_at.isoformat(),
                        canonical_json(draft.actor.model_dump(mode="json")),
                        canonical_json(draft.payload),
                        previous_hash,
                        event_hash,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        return LedgerEvent(
            event_id=draft.event_id,
            sequence=sequence,
            stream_id=draft.stream_id,
            event_type=draft.event_type,
            occurred_at=draft.occurred_at,
            actor=draft.actor,
            payload=draft.payload,
            previous_event_hash=previous_hash,
            event_hash=event_hash,
        )

    def events(self) -> list[LedgerEvent]:
        events: list[LedgerEvent] = []
        with self._read_connection() as connection:
            for row in connection.execute("SELECT * FROM events ORDER BY sequence"):
                sequence = row["sequence"]
                try:
                    events.append(self._row_to_event(row))
                except _MALFORMED_ROW_ERRORS as exc:
                    raise LedgerReadError(
                        f"malformed event row at sequence {sequence}: {exc}"
                    ) from exc
        return events

    def verify(self) -> LedgerVerification:
        previous_hash: str | None = None
        checked_events = 0

        try:
            with self._read_connection() as connection:
                cursor = connection.execute("SELECT * FROM events ORDER BY sequence")
                for row in cursor:
                    sequence = row["sequence"]
                    try:
                        event = self._row_to_event(row)
                        draft = EventDraft(
                            event_id=event.event_id,
                            stream_id=event.stream_id,
                            event_type=event.event_type,
                            occurred_at=event.occurred_at,
                            actor=event.actor,
                            payload=event.payload,
                        )
                        expected_hash = compute_event_hash(
                            draft,
                            sequence=event.sequence,
                            previous_event_hash=previous_hash,
                        )
                    except _MALFORMED_ROW_ERRORS as exc:
                        return LedgerVerification(
                            valid=False,
                            checked_events=checked_events,
                            failure_sequence=(
                                int(sequence) if isinstance(sequence, int) else None
                            ),
                            reason=f"malformed event row: {exc}",
                        )

                    if event.previous_event_hash != previous_hash:
                        return LedgerVerification(
                            valid=False,
                            checked_events=checked_events,
                            failure_sequence=event.sequence,
                            reason="previous event hash does not match chain head",
                        )

                    if event.event_hash != expected_hash:
                        return LedgerVerification(
                            valid=False,
                            checked_events=checked_events,
                            failure_sequence=event.sequence,
                            reason="event hash does not match canonical event content",
                        )

                    previous_hash = event.event_hash
                    checked_events += 1
        except LedgerReadError as exc:
            return LedgerVerification(
                valid=False,
                checked_events=checked_events,
                reason=str(exc),
            )

        return LedgerVerification(valid=True, checked_events=checked_events)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        marker_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'sayf_ledger_meta'
            """
        ).fetchone()
        if marker_table is None:
            raise LedgerReadError("Sayf ledger schema marker is missing")

        marker = connection.execute(
            "SELECT value FROM sayf_ledger_meta WHERE key = 'schema_version'"
        ).fetchone()
        if marker is None or str(marker["value"]) != _LEDGER_SCHEMA_VERSION:
            raise LedgerReadError("Sayf ledger schema version is unsupported")

        event_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        if event_table is None:
            raise LedgerReadError("Sayf ledger events table is missing")

        column_rows = connection.execute("PRAGMA table_info(events)").fetchall()
        columns = {str(row["name"]): row for row in column_rows}
        if set(columns) != set(_EXPECTED_EVENT_COLUMNS):
            raise LedgerReadError("Sayf ledger events schema has unexpected columns")

        for name, (expected_type, required_not_null, required_pk) in (
            _EXPECTED_EVENT_COLUMNS.items()
        ):
            row = columns[name]
            if str(row["type"]).upper() != expected_type:
                raise LedgerReadError(f"Sayf ledger column {name} has unexpected type")
            if required_not_null and int(row["notnull"]) != 1:
                raise LedgerReadError(f"Sayf ledger column {name} must be NOT NULL")
            if required_pk and int(row["pk"]) != 1:
                raise LedgerReadError(f"Sayf ledger column {name} must be PRIMARY KEY")
            if not required_pk and int(row["pk"]) != 0:
                raise LedgerReadError(f"Sayf ledger column {name} has unexpected PK role")

        unique_column_sets: set[frozenset[str]] = set()
        indexes = connection.execute("PRAGMA index_list(events)").fetchall()
        for index in indexes:
            if int(index["unique"]) != 1:
                continue
            index_name = str(index["name"]).replace("'", "''")
            index_columns = connection.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
            unique_column_sets.add(
                frozenset(str(row["name"]) for row in index_columns)
            )
        if not _EXPECTED_UNIQUE_COLUMN_SETS.issubset(unique_column_sets):
            raise LedgerReadError("Sayf ledger unique constraints are incomplete")

        stream_index = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_events_stream_sequence'
            """
        ).fetchone()
        if stream_index is None:
            raise LedgerReadError("Sayf ledger stream index is missing")
        stream_index_columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA index_info('idx_events_stream_sequence')"
            ).fetchall()
        ]
        if stream_index_columns != ["stream_id", "sequence"]:
            raise LedgerReadError("Sayf ledger stream index is malformed")

        triggers = {
            str(row["name"]): str(row["sql"] or "").lower()
            for row in connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'events'
                """
            ).fetchall()
        }
        for name, required_clause in _EXPECTED_IMMUTABILITY_TRIGGERS.items():
            sql = " ".join(triggers.get(name, "").split())
            if required_clause not in sql or "raise(abort" not in sql:
                raise LedgerReadError(f"Sayf ledger trigger {name} is missing or malformed")

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(
            event_id=str(row["event_id"]),
            sequence=int(row["sequence"]),
            stream_id=str(row["stream_id"]),
            event_type=str(row["event_type"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            actor=Actor.model_validate(json.loads(str(row["actor_json"]))),
            payload=json.loads(str(row["payload_json"])),
            previous_event_hash=row["previous_event_hash"],
            event_hash=str(row["event_hash"]),
        )
