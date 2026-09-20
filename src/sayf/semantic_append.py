from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from sayf.domain import EventDraft, LedgerEvent
from sayf.hashing import canonical_json, compute_event_hash
from sayf.storage import LedgerReadError, SQLiteEventStore


def append_if_ledger_head(
    store: SQLiteEventStore,
    draft: EventDraft,
    *,
    expected_event_count: int,
    expected_head_hash: str | None,
) -> LedgerEvent:
    """Append only if the verified ledger head still matches a semantic snapshot.

    M0.2 derives typed semantics from a complete verified ledger snapshot before it
    creates a typed record or relation. The generic M0.1 append path independently
    verifies local history, but that alone cannot prove that the semantic snapshot
    used by the caller is still current. This helper binds the write to that exact
    sequence/head pair under the same ``BEGIN IMMEDIATE`` transaction that appends
    the event.
    """
    if expected_event_count < 0:
        raise ValueError("expected_event_count must be non-negative")
    if expected_event_count == 0 and expected_head_hash is not None:
        raise ValueError("an empty expected ledger cannot have a head hash")
    if expected_event_count > 0 and expected_head_hash is None:
        raise ValueError("a non-empty expected ledger must have a head hash")

    try:
        snapshot = EventDraft.model_validate(draft.model_dump(mode="python"))
        actor_json = canonical_json(snapshot.actor.model_dump(mode="json"))
        payload_json = canonical_json(snapshot.payload)
    except (ValidationError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise LedgerReadError(f"event draft is not canonical JSON: {exc}") from exc

    try:
        try:
            store.initialize()
        except LedgerReadError as exc:
            raise LedgerReadError(f"unable to append to ledger: {exc}") from exc

        with store._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                store._validate_database(connection)
                verification, _, previous_hash = store._verify_connection(connection)
                if not verification.valid:
                    raise LedgerReadError(store._verification_error(verification))

                if (
                    verification.checked_events != expected_event_count
                    or previous_hash != expected_head_hash
                ):
                    raise LedgerReadError(
                        "semantic append precondition failed: ledger head changed "
                        "after typed-state validation; retry the typed operation"
                    )

                duplicate_event_id = connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ?",
                    (snapshot.event_id,),
                ).fetchone()
                if duplicate_event_id is not None:
                    raise LedgerReadError(
                        "unable to append to ledger: UNIQUE constraint failed: events.event_id"
                    )

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
