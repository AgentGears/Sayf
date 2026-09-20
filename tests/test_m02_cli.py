from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sayf.cli import app

runner = CliRunner()


def test_cli_record_relation_graph_and_artifact_flow(tmp_path: Path) -> None:
    db = tmp_path / ".sayf" / "ledger.sqlite3"
    objects = tmp_path / ".sayf" / "objects"

    claim_result = runner.invoke(
        app,
        [
            "record",
            "create",
            "--type",
            "Claim",
            "--id",
            "rec_claim",
            "--payload",
            '{"text":"supported claim"}',
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert claim_result.exit_code == 0, claim_result.output
    assert json.loads(claim_result.stdout)["id"] == "rec_claim"

    evidence_result = runner.invoke(
        app,
        [
            "record",
            "create",
            "--type",
            "Evidence",
            "--id",
            "rec_evidence",
            "--payload",
            '{"kind":"test"}',
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert evidence_result.exit_code == 0, evidence_result.output

    relation_result = runner.invoke(
        app,
        [
            "relation",
            "create",
            "--type",
            "supports",
            "--source",
            "rec_evidence",
            "--target",
            "rec_claim",
            "--id",
            "rel_support",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert relation_result.exit_code == 0, relation_result.output

    path_result = runner.invoke(
        app,
        [
            "graph",
            "path",
            "rec_evidence",
            "rec_claim",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert path_result.exit_code == 0, path_result.output
    paths = json.loads(path_result.stdout)
    assert paths[0]["steps"][0]["relation"]["id"] == "rel_support"

    source = tmp_path / "receipt.txt"
    source.write_bytes(b"receipt\n")
    artifact_result = runner.invoke(
        app,
        [
            "artifact",
            "put",
            str(source),
            "--id",
            "rec_artifact",
            "--media-type",
            "text/plain",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert artifact_result.exit_code == 0, artifact_result.output
    artifact = json.loads(artifact_result.stdout)
    assert artifact["record"]["type"] == "Artifact"

    verify_artifact = runner.invoke(
        app,
        [
            "artifact",
            "verify",
            "rec_artifact",
            "--db",
            str(db),
            "--objects",
            str(objects),
        ],
    )
    assert verify_artifact.exit_code == 0, verify_artifact.output
    assert json.loads(verify_artifact.stdout)["valid"] is True

    verify_ledger = runner.invoke(app, ["ledger", "verify", "--db", str(db)])
    assert verify_ledger.exit_code == 0, verify_ledger.output
    assert json.loads(verify_ledger.stdout)["checked_events"] == 4


def test_cli_rejects_reserved_typed_event_through_raw_append(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    result = runner.invoke(
        app,
        [
            "ledger",
            "append",
            "--type",
            "sayf.record.created.v1",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code != 0
    assert "typed Sayf event types are reserved" in result.output
    assert db.exists() is False
