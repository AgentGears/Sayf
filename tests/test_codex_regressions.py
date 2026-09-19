from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from sayf import cli as cli_module
from sayf.cli import _parse_payload, app
from sayf.storage import SQLiteEventStore

runner = CliRunner()


def _force_json_recursion(monkeypatch) -> None:
    def recursive_loads(*args, **kwargs):
        raise RecursionError("forced JSON decoder recursion")

    monkeypatch.setattr(
        cli_module,
        "json",
        SimpleNamespace(loads=recursive_loads, JSONDecodeError=json.JSONDecodeError),
    )


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


def test_parse_payload_converts_recursion_to_bad_parameter(monkeypatch) -> None:
    _force_json_recursion(monkeypatch)

    with pytest.raises(typer.BadParameter, match="payload is not valid strict JSON"):
        _parse_payload("{}")


def test_cli_converts_json_recursion_without_traceback(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.sqlite3"
    _force_json_recursion(monkeypatch)

    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "RecordCreated",
            "--payload",
            "{}",
            "--db",
            str(db),
        ],
    )

    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert db.exists() is False
