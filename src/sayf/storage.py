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

_SCHEMA = """
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

_REQUIRED_EVENT_COLUMNS = {
    "sequence",
    "event_id",
    "stream_id",
    "event_type",
    "occurred_at",
    "actor_json",
    "payload_json",
    "previous_event_hash",
    "event_hash",
}


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
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        return [self._row_to_event(row) for row in rows]

    def verify(self) -> LedgerVerification:
        if not self.path.exists():
            return LedgerVerification(
                valid=False,
                checked_events=0,
                reason="ledger database does not exist",
            )
        if not self.path.is_file():
            return LedgerVerification(
                valid=False,
                checked_events=0,
                reason="ledger database path is not a file",
            )

        try:
            with self._connect(read_only=True) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
                ).fetchone()
                if table is None:
                    return LedgerVerification(
                        valid=False,
                        checked_events=0,
                        reason="Sayf ledger schema is not initialized",
                    )

                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(events)").fetchall()
                }
                if not _REQUIRED_EVENT_COLUMNS.issubset(columns):
                    return LedgerVerification(
                        valid=False,
                        checked_events=0,
                        reason="Sayf ledger schema is incomplete",
                    )

                rows = connection.execute(
                    "SELECT * FROM events ORDER BY sequence"
                ).fetchall()
        except sqlite3.Error as exc:
            return LedgerVerification(
                valid=False,
                checked_events=0,
                reason=f"unable to read ledger database: {exc}",
            )

        previous_hash: str | None = None
        checked_events = 0

        for row in rows:
            sequence = int(row["sequence"])
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
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                return LedgerVerification(
                    valid=False,
                    checked_events=checked_events,
                    failure_sequence=sequence,
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

        return LedgerVerification(valid=True, checked_events=checked_events)

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
