from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sayf.cli import app

runner = CliRunner()


def _record(db: Path, objects: Path, record_id: str, record_type: str) -> None:
    result = runner.invoke(
        app,
        [
            "record",
            "create",
            "--type",
            record_type,
            "--id",
            record_id,
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert result.exit_code == 0, result.output


def _relation(
    db: Path,
    objects: Path,
    relation_id: str,
    relation_type: str,
    source_id: str,
    target_id: str,
) -> None:
    result = runner.invoke(
        app,
        [
            "relation",
            "create",
            "--type",
            relation_type,
            "--source",
            source_id,
            "--target",
            target_id,
            "--id",
            relation_id,
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert result.exit_code == 0, result.output


def _state_command(db: Path, objects: Path, command: str, relation_id: str) -> None:
    result = runner.invoke(
        app,
        [
            "state",
            command,
            relation_id,
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_m03_assumption_intent_invalidation_supersession_slice(
    tmp_path: Path,
) -> None:
    db = tmp_path / ".sayf" / "ledger.sqlite3"
    objects = tmp_path / ".sayf" / "objects"

    _record(db, objects, "a1", "Assumption")
    _record(db, objects, "i1", "IntentRevision")
    _record(db, objects, "o1", "Observation")
    _record(db, objects, "i2", "IntentRevision")

    _relation(db, objects, "dep_i1_a1", "depends_on", "i1", "a1")
    _relation(db, objects, "inv_o1_a1", "invalidates", "o1", "a1")
    _relation(db, objects, "sup_i2_i1", "supersedes", "i2", "i1")

    _state_command(db, objects, "bind-dependency", "dep_i1_a1")
    _state_command(db, objects, "invalidate", "inv_o1_a1")

    affected = runner.invoke(
        app,
        [
            "state",
            "affected",
            "a1",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert affected.exit_code == 0, affected.output
    affected_data = json.loads(affected.stdout)
    assert [item["record_id"] for item in affected_data] == ["i1"]
    assert affected_data[0]["freshness"] == "stale"
    assert affected_data[0]["stale_causes"][0]["root_record_id"] == "a1"

    _state_command(db, objects, "supersede", "sup_i2_i1")

    old_state = runner.invoke(
        app,
        [
            "state",
            "show",
            "i1",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert old_state.exit_code == 0, old_state.output
    old_data = json.loads(old_state.stdout)
    assert old_data["revision"] == "superseded"
    assert old_data["superseded_by_record_id"] == "i2"
    assert old_data["freshness"] == "stale"

    new_state = runner.invoke(
        app,
        [
            "state",
            "show",
            "i2",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert new_state.exit_code == 0, new_state.output
    new_data = json.loads(new_state.stdout)
    assert new_data["revision"] == "current"
    assert new_data["freshness"] == "fresh"


def test_cli_rejects_reserved_m03_event_through_raw_append(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "sayf.record.invalidated.v1",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code != 0
    assert "typed Sayf event types are reserved" in result.output
    assert db.exists() is False
