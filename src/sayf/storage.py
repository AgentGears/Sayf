from __future__ import annotations

import json
import os
import sqlite3
import tempfile
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

_INSERT_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS events_no_replace
BEFORE INSERT ON events
WHEN EXISTS (
    SELECT 1 FROM events
    WHERE sequence = NEW.sequence
       OR event_id = NEW.event_id
       OR event_hash = NEW.event_hash
)
BEGIN
    SELECT RAISE(ABORT, '{_IMMUTABILITY_MESSAGE}');
END
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
{_INSERT_TRIGGER_SQL};
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
    ("trigger", "events_no_replace"): _normalize_schema_sql(_INSERT_TRIGGER_SQL),
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
        mode = "ro" if read_only else "rw"
        database = f"{self.path.resolve().as_uri()}?mode={mode}"
        connection = sqlite3.connect(
            database,
            timeout=30.0,
            isolation_level=None,
            uri=True,
        )
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
                self._validate_database(connection)
                yield connection
        except LedgerReadError:
            raise
        except sqlite3.Error as exc:
            raise LedgerReadError(f"unable to read ledger database: {exc}") from exc

    def initialize(self) -> None:
        candidate: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                if not self.path.is_file():
                    raise LedgerReadError("ledger database path is not a file")
                self._validate_existing_ledger()
                return

            fd, candidate_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.init-",
                suffix=".sqlite3",
            )
            os.close(fd)
            candidate = Path(candidate_name)
            candidate_store = SQLiteEventStore(candidate)

            with candidate_store._connect() as connection:
                connection.executescript(_SCHEMA)
                candidate_store._validate_database(connection)
                verification, _, _ = candidate_store._verify_connection(connection)
                if not verification.valid:
                    raise LedgerReadError(self._verification_error(verification))

            try:
                os.link(candidate, self.path)
            except FileExistsError:
                self._validate_existing_ledger()
                return
            except OSError as exc:
                raise LedgerReadError(
                    f"unable to install new ledger atomically: {exc}"
                ) from exc

            self._validate_existing_ledger()
        except LedgerReadError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise LedgerReadError(f"unable to initialize ledger: {exc}") from exc
        finally:
            if candidate is not None:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

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
                    self._validate_database(connection)
                    verification, _, previous_hash = self._verify_connection(connection)
                    if not verification.valid:
                        raise LedgerReadError(self._verification_error(verification))

                    sequence = verification.checked_events + 1
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
        with self._read_connection() as connection:
            verification, events, _ = self._verify_connection(
                connection,
                collect_events=True,
            )
        if not verification.valid:
            raise LedgerReadError(self._verification_error(verification))
        return events

    def verify(self) -> LedgerVerification:
        try:
            with self._read_connection() as connection:
                verification, _, _ = self._verify_connection(connection)
                return verification
        except LedgerReadError as exc:
            return LedgerVerification(
                valid=False,
                checked_events=0,
                reason=str(exc),
            )

    def _validate_existing_ledger(self) -> None:
        with self._read_connection() as connection:
            verification, _, _ = self._verify_connection(connection)
        if not verification.valid:
            raise LedgerReadError(self._verification_error(verification))

    def _verify_connection(
        self,
        connection: sqlite3.Connection,
        *,
        collect_events: bool = False,
    ) -> tuple[LedgerVerification, list[LedgerEvent], str | None]:
        previous_hash: str | None = None
        checked_events = 0
        events: list[LedgerEvent] = []
        cursor = connection.execute("SELECT * FROM events ORDER BY sequence")

        for row in cursor:
            sequence = row["sequence"]
            try:
                raw_occurred_at = self._required_text(row, "occurred_at")
                raw_actor_json = self._required_text(row, "actor_json")
                raw_payload_json = self._required_text(row, "payload_json")
                event = self._row_to_event(row)
                canonical_actor_json = canonical_json(event.actor.model_dump(mode="json"))
                canonical_payload_json = canonical_json(event.payload)
            except _MALFORMED_ROW_ERRORS as exc:
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=(int(sequence) if type(sequence) is int else None),
                        reason=f"malformed event row: {exc}",
                    ),
                    events,
                    previous_hash,
                )

            expected_sequence = checked_events + 1
            if event.sequence != expected_sequence:
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason=(
                            "event sequence is not contiguous: "
                            f"expected {expected_sequence}, found {event.sequence}"
                        ),
                    ),
                    events,
                    previous_hash,
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
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason=f"malformed event row: {exc}",
                    ),
                    events,
                    previous_hash,
                )

            if event.previous_event_hash != previous_hash:
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason="previous event hash does not match chain head",
                    ),
                    events,
                    previous_hash,
                )

            if event.event_hash != expected_hash:
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason="event hash does not match canonical event content",
                    ),
                    events,
                    previous_hash,
                )

            if raw_occurred_at != event.occurred_at.isoformat():
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason="stored event timestamp is not canonical",
                    ),
                    events,
                    previous_hash,
                )
            if raw_actor_json != canonical_actor_json:
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason="stored actor JSON is not canonical",
                    ),
                    events,
                    previous_hash,
                )
            if raw_payload_json != canonical_payload_json:
                return (
                    LedgerVerification(
                        valid=False,
                        checked_events=checked_events,
                        failure_sequence=event.sequence,
                        reason="stored payload JSON is not canonical",
                    ),
                    events,
                    previous_hash,
                )

            previous_hash = event.event_hash
            checked_events += 1
            if collect_events:
                events.append(event)

        return (
            LedgerVerification(valid=True, checked_events=checked_events),
            events,
            previous_hash,
        )

    @staticmethod
    def _verification_error(verification: LedgerVerification) -> str:
        if verification.failure_sequence is not None:
            return (
                f"ledger verification failed at sequence {verification.failure_sequence}: "
                f"{verification.reason}"
            )
        return f"ledger verification failed: {verification.reason}"

    @staticmethod
    def _validate_database(connection: sqlite3.Connection) -> None:
        SQLiteEventStore._validate_sqlite_integrity(connection)
        SQLiteEventStore._validate_schema(connection)

    @staticmethod
    def _validate_sqlite_integrity(connection: sqlite3.Connection) -> None:
        results = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
        if results != ["ok"]:
            detail = "; ".join(results) if results else "no result"
            raise LedgerReadError(f"SQLite quick_check failed: {detail}")

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

        metadata_rows = connection.execute(
            "SELECT key, value FROM sayf_ledger_meta ORDER BY key"
        ).fetchall()
        metadata = [(str(row["key"]), str(row["value"])) for row in metadata_rows]
        if len(metadata) != 1 or metadata[0][0] != "schema_version":
            raise LedgerReadError("Sayf ledger metadata is unsupported or malformed")
        if metadata[0][1] != _LEDGER_SCHEMA_VERSION:
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
    def _required_text(row: sqlite3.Row, column: str) -> str:
        value = row[column]
        if not isinstance(value, str):
            raise ValueError(f"stored {column} must be text")
        return value

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
        sequence = row["sequence"]
        if type(sequence) is not int:
            raise ValueError("stored sequence must be an integer")

        previous_hash = row["previous_event_hash"]
        if previous_hash is not None and not isinstance(previous_hash, str):
            raise ValueError("stored previous_event_hash must be text or null")

        event_id = SQLiteEventStore._required_text(row, "event_id")
        stream_id = SQLiteEventStore._required_text(row, "stream_id")
        event_type = SQLiteEventStore._required_text(row, "event_type")
        occurred_at = SQLiteEventStore._required_text(row, "occurred_at")
        actor_json = SQLiteEventStore._required_text(row, "actor_json")
        payload_json = SQLiteEventStore._required_text(row, "payload_json")
        event_hash = SQLiteEventStore._required_text(row, "event_hash")

        return LedgerEvent(
            event_id=event_id,
            sequence=sequence,
            stream_id=stream_id,
            event_type=event_type,
            occurred_at=datetime.fromisoformat(occurred_at),
            actor=Actor.model_validate(json.loads(actor_json)),
            payload=json.loads(payload_json),
            previous_event_hash=previous_hash,
            event_hash=event_hash,
        )
