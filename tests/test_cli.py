from __future__ import annotations

import json
import sqlite3

from typer.testing import CliRunner

from sayf.cli import app

runner = CliRunner()


def test_cli_init_append_show_and_verify(tmp_path) -> None:
    root = tmp_path / "project"
    db = root / ".sayf" / "ledger.sqlite3"

    init_result = runner.invoke(app, ["init", str(root)])
    assert init_result.exit_code == 0
    assert db.is_file()

    append_result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "RecordCreated",
            "--stream",
            "project:test",
            "--actor-kind",
            "human",
            "--actor-id",
            "tester",
            "--payload",
            '{"record_id":"r1","flags":[true,null]}',
            "--db",
            str(db),
        ],
    )
    assert append_result.exit_code == 0
    appended = json.loads(append_result.stdout)
    assert appended["sequence"] == 1
    assert appended["payload"] == {"record_id": "r1", "flags": [True, None]}

    show_result = runner.invoke(app, ["ledger", "show", "--db", str(db)])
    assert show_result.exit_code == 0
    shown = json.loads(show_result.stdout)
    assert len(shown) == 1
    assert shown[0]["event_id"] == appended["event_id"]

    verify_result = runner.invoke(app, ["ledger", "verify", "--db", str(db)])
    assert verify_result.exit_code == 0
    verification = json.loads(verify_result.stdout)
    assert verification == {
        "valid": True,
        "checked_events": 1,
        "failure_sequence": None,
        "reason": None,
    }


def test_cli_read_commands_do_not_create_missing_database(tmp_path) -> None:
    db = tmp_path / "missing.sqlite3"

    show_result = runner.invoke(app, ["ledger", "show", "--db", str(db)])
    assert show_result.exit_code == 1
    assert "ledger database does not exist" in show_result.output
    assert db.exists() is False

    verify_result = runner.invoke(app, ["ledger", "verify", "--db", str(db)])
    assert verify_result.exit_code == 1
    verification = json.loads(verify_result.stdout)
    assert verification["valid"] is False
    assert verification["reason"] == "ledger database does not exist"
    assert db.exists() is False


def test_cli_init_refuses_existing_non_sayf_database_without_mutation(tmp_path) -> None:
    root = tmp_path / "project"
    db = root / ".sayf" / "ledger.sqlite3"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()

    result = runner.invoke(app, ["init", str(root)])

    assert result.exit_code == 1
    assert "Sayf ledger schema marker is missing" in result.output
    with sqlite3.connect(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"unrelated"}


def test_cli_append_rejects_non_standard_json_before_creating_database(tmp_path) -> None:
    db = tmp_path / "ledger.sqlite3"

    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "RecordCreated",
            "--payload",
            '{"score":NaN}',
            "--db",
            str(db),
        ],
    )

    assert result.exit_code != 0
    assert "payload is not valid strict JSON" in result.output
    assert db.exists() is False


def test_cli_append_rejects_duplicate_json_keys(tmp_path) -> None:
    db = tmp_path / "ledger.sqlite3"

    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "RecordCreated",
            "--payload",
            '{"record_id":"r1","record_id":"r2"}',
            "--db",
            str(db),
        ],
    )

    assert result.exit_code != 0
    assert "duplicate JSON object key" in result.output
    assert db.exists() is False


def test_cli_append_reports_invalid_event_fields_without_traceback(tmp_path) -> None:
    db = tmp_path / "ledger.sqlite3"

    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "",
            "--actor-id",
            "",
            "--db",
            str(db),
        ],
    )

    assert result.exit_code != 0
    assert "event fields are invalid" in result.output
    assert "Traceback" not in result.output
    assert db.exists() is False
