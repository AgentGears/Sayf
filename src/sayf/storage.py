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
_IMMUTABILITY_MESSAGE = "Sayf ledger events are immutable"

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sayf_ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_EVENTS_TABLE_SQL = """
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
)
"""

_STREAM_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_events_stream_sequence
    ON events(stream_id, sequence)
"""

_UPDATE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, '{_IMMUTABILITY_MESSAGE}');
END
"""

_DELETE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, '{_IMMUTABILITY_MESSAGE}');
END
"""

_SCHEMA = f"""
{_META_TABLE_SQL};

INSERT OR IGNORE INTO sayf_ledger_meta (key, value)
VALUES ('schema_version', '{_LEDGER_SCHEMA_VERSION}');

{_EVENTS_TABLE_SQL};
{_STREAM_INDEX_SQL};
{_UPDATE_TRIGGER_SQL};
{_DELETE_TRIGGER_SQL};
"""


def _normalize_schema_sql(sql: str) -> str:
    normalized = " ".join(sql.strip().split()).lower()
    for object_type in ("table", "index", "trigger"):
        normalized = normalized.replace(
            f"create {object_type} if not exists ",
            f"create {object_type} ",
        )
    return normalized.replace(";", "")


_EXPECTED_SCHEMA_OBJECTS = {
    ("table", "sayf_ledger_meta"): _normalize_schema_sql(_META_TABLE_SQL),
    ("table", "events"): _normalize_schema_sql(_EVENTS_TABLE_SQL),
    ("index", "idx_events_stream_sequence"): _normalize_schema_sql(_STREAM_INDEX_SQL),
    ("trigger", "events_no_update"): _normalize_schema_sql(_UPDATE_TRIGGER_SQL),
    ("trigger", "events_no_delete"): _normalize_schema_sql(_DELETE_TRIGGER_SQL),
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
    """Raised when a Sayf ledger cannot be used safely with this runtime."""


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
        created_new_file = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                if not self.path.is_file():
                    raise LedgerReadError("ledger database path is not a file")
                with self._read_connection():
                    return

            try:
                with self.path.open("xb"):
                    pass
                created_new_file = True
            except FileExistsError:
                if not self.path.is_file():
                    raise LedgerReadError("ledger database path is not a file") from None
                with self._read_connection():
                    return

            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                self._validate_schema(connection)
        except LedgerReadError:
            if created_new_file:
                self._remove_failed_initialization()
            raise
        except (OSError, sqlite3.Error) as exc:
            if created_new_file:
                self._remove_failed_initialization()
            raise LedgerReadError(f"unable to initialize ledger: {exc}") from exc

    def append(self, draft: EventDraft) -> LedgerEvent:
        try:
            snapshot = EventDraft.model_validate(draft.model_dump(mode="python"))
            actor_json = canonical_json(snapshot.actor.model_dump(mode="json"))
            payload_json = canonical_json(snapshot.payload)
        except (ValidationError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise LedgerReadError(f"event draft is not canonical JSON: {exc}") from exc

        try:
            if not self.path.exists():
                self.initialize()
            elif not self.path.is_file():
                raise LedgerReadError("ledger database path is not a file")

            with self._connect() as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._validate_schema(connection)
                    row = connection.execute(
                        "SELECT sequence, event_hash FROM events ORDER BY sequence DESC LIMIT 1"
                    ).fetchone()
                    sequence = 1 if row is None else int(row["sequence"]) + 1
                    previous_hash = None if row is None else str(row["event_hash"])
                    event_hash = compute_event_hash(
                        snapshot,
                        sequence=sequence,
                        previous_event_hash=previous_hash,
                    )

                    cursor = connection.execute(
                        """
                        INSERT INTO events (
                            sequence, event_id, stream_id, event_type, occurred_at,
                            actor_json, payload_json, previous_event_hash, event_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sequence,
                            snapshot.event_id,
                            snapshot.stream_id,
                            snapshot.event_type,
                            snapshot.occurred_at.isoformat(),
                            actor_json,
                            payload_json,
                            previous_hash,
                            event_hash,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise LedgerReadError("ledger append did not persist exactly one event")
                    connection.execute("COMMIT")
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        except LedgerReadError:
            raise
        except sqlite3.Error as exc:
            raise LedgerReadError(f"unable to append to ledger: {exc}") from exc

        return LedgerEvent(
            event_id=snapshot.event_id,
            sequence=sequence,
            stream_id=snapshot.stream_id,
            event_type=snapshot.event_type,
            occurred_at=snapshot.occurred_at,
            actor=snapshot.actor,
            payload=snapshot.payload,
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
                    except _MALFORMED_ROW_ERRORS as exc:
                        return LedgerVerification(
                            valid=False,
                            checked_events=checked_events,
                            failure_sequence=(
                                int(sequence) if isinstance(sequence, int) else None
                            ),
                            reason=f"malformed event row: {exc}",
                        )

                    expected_sequence = checked_events + 1
                    if event.sequence != expected_sequence:
                        return LedgerVerification(
                            valid=False,
                            checked_events=checked_events,
                            failure_sequence=event.sequence,
                            reason=(
                                "event sequence is not contiguous: "
                                f"expected {expected_sequence}, found {event.sequence}"
                            ),
                        )

                    try:
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
                            failure_sequence=event.sequence,
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

    def _remove_failed_initialization(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE type = 'table' AND name = 'sayf_ledger_meta'
            """
        ).fetchone()
        if marker is None:
            raise LedgerReadError("Sayf ledger schema marker is missing")
        if _normalize_schema_sql(str(marker["sql"] or "")) != _EXPECTED_SCHEMA_OBJECTS[
            ("table", "sayf_ledger_meta")
        ]:
            raise LedgerReadError("Sayf ledger schema marker is malformed")

        marker_row = connection.execute(
            "SELECT value FROM sayf_ledger_meta WHERE key = 'schema_version'"
        ).fetchone()
        if marker_row is None or str(marker_row["value"]) != _LEDGER_SCHEMA_VERSION:
            raise LedgerReadError("Sayf ledger schema version is unsupported")

        rows = connection.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'trigger', 'view')
            """
        ).fetchall()
        actual_objects = {
            (str(row["type"]), str(row["name"])): _normalize_schema_sql(
                str(row["sql"] or "")
            )
            for row in rows
        }

        if set(actual_objects) != set(_EXPECTED_SCHEMA_OBJECTS):
            raise LedgerReadError("Sayf ledger schema objects are unexpected or incomplete")

        for key, expected_sql in _EXPECTED_SCHEMA_OBJECTS.items():
            if actual_objects[key] != expected_sql:
                raise LedgerReadError(
                    f"Sayf ledger schema object {key[1]} is missing or malformed"
                )

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
