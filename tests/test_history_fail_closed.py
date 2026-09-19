from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from sayf.cli import app
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import LedgerReadError, SQLiteEventStore

runner = CliRunner()


def _draft(event_type: str, record_id: str) -> EventDraft:
    return EventDraft(
        stream_id="project:test",
        event_type=event_type,
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        payload={"record_id": record_id},
    )


def _corrupt_first_event_and_restore_guard(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE sequence = 1",
            (json.dumps({"record_id": "rewritten"}),),
        )
        connection.executescript(
            """
            CREATE TRIGGER events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
            END;
            """
        )
        connection.commit()


def test_events_refuses_hash_invalid_history(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", "r1"))
    _corrupt_first_event_and_restore_guard(path)

    with pytest.raises(
        LedgerReadError,
        match="ledger verification failed at sequence 1",
    ):
        store.events()


def test_append_refuses_to_extend_hash_invalid_history(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", "r1"))
    _corrupt_first_event_and_restore_guard(path)

    with pytest.raises(
        LedgerReadError,
        match="ledger verification failed at sequence 1",
    ):
        store.append(_draft("RecordCreated", "r2"))

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1


def test_cli_show_refuses_hash_invalid_history_without_traceback(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", "r1"))
    _corrupt_first_event_and_restore_guard(path)

    result = runner.invoke(app, ["ledger", "show", "--db", str(path)])

    assert result.exit_code == 1
    assert "ledger verification failed at sequence 1" in result.output
    assert "Traceback" not in result.output


def test_verify_still_reports_structured_failure_for_hash_invalid_history(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft("RecordCreated", "r1"))
    _corrupt_first_event_and_restore_guard(path)

    result = store.verify()

    assert result.valid is False
    assert result.checked_events == 0
    assert result.failure_sequence == 1
    assert result.reason == "event hash does not match canonical event content"
