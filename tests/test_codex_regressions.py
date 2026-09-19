from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from sayf.cli import app
from sayf.storage import SQLiteEventStore

runner = CliRunner()


@pytest.mark.parametrize("sequence", [0, -1])
def test_verify_nonpositive_sequence_fails_closed(tmp_path, sequence: int) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO events (
                sequence, event_id, stream_id, event_type, occurred_at,
                actor_json, payload_json, previous_event_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                f"evt_invalid_{sequence}",
                "project:test",
                "RecordCreated",
                "2026-09-20T00:00:00+00:00",
                '{"display_name":null,"id":"tester","kind":"human","metadata":{}}',
                "{}",
                None,
                f"sha256:invalid-{sequence}",
            ),
        )
        connection.commit()

    result = store.verify()

    assert result.valid is False
    assert result.checked_events == 0
    assert result.failure_sequence == sequence
    assert result.reason is not None
    assert result.reason.startswith("malformed event row:")


def test_cli_rejects_deeply_nested_payload_without_traceback(tmp_path) -> None:
    db = tmp_path / "ledger.sqlite3"
    payload = '{"value":' + "[" * 2000 + "0" + "]" * 2000 + "}"

    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "RecordCreated",
            "--payload",
            payload,
            "--db",
            str(db),
        ],
    )

    assert result.exit_code != 0
    assert "payload is not valid strict JSON" in result.output
    assert "Traceback" not in result.output
    assert db.exists() is False
