from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sayf.cli import app

runner = CliRunner()


def invoke(root: Path, *args: str):
    return runner.invoke(
        app,
        [
            *args,
            "--db",
            str(root / ".sayf" / "ledger.sqlite3"),
            "--objects",
            str(root / ".sayf" / "objects"),
        ],
    )


def test_cli_m04_permit_then_subject_supersession_stales_decision(
    tmp_path: Path,
) -> None:
    for record_type, record_id in (
        ("ChangeSet", "change1"),
        ("Evidence", "evidence1"),
    ):
        result = invoke(
            tmp_path,
            "record",
            "create",
            "--type",
            record_type,
            "--id",
            record_id,
        )
        assert result.exit_code == 0, result.output

    verification = invoke(
        tmp_path,
        "verification",
        "record",
        "--id",
        "receipt1",
        "--payload",
        json.dumps(
            {
                "subject_id": "change1",
                "contract": "tests",
                "result": "pass",
                "evidence_record_ids": ["evidence1"],
                "environment": {"platform": "cli-test"},
                "independent_from_generation": "not_applicable",
                "verified_claim": "tests passed for the bound change",
            }
        ),
    )
    assert verification.exit_code == 0, verification.output

    policy = invoke(
        tmp_path,
        "policy",
        "register",
        "--id",
        "policy1",
        "--payload",
        json.dumps(
            {
                "name": "release-gate",
                "requirements": [{"contract": "tests", "minimum_passes": 1}],
            }
        ),
    )
    assert policy.exit_code == 0, policy.output

    request = invoke(
        tmp_path,
        "gate",
        "request",
        "--id",
        "request1",
        "--payload",
        json.dumps(
            {
                "subject_id": "change1",
                "policy_snapshot_id": "policy1",
                "verification_receipt_ids": ["receipt1"],
            }
        ),
    )
    assert request.exit_code == 0, request.output

    decision = invoke(
        tmp_path,
        "gate",
        "evaluate",
        "request1",
        "--id",
        "decision1",
    )
    assert decision.exit_code == 0, decision.output
    assert json.loads(decision.output)["payload"]["outcome"] == "permit"

    status = invoke(tmp_path, "gate", "show", "decision1")
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["freshness"] == "fresh"

    result = invoke(
        tmp_path,
        "record",
        "create",
        "--type",
        "ChangeSet",
        "--id",
        "change2",
    )
    assert result.exit_code == 0, result.output
    result = invoke(
        tmp_path,
        "relation",
        "create",
        "--type",
        "supersedes",
        "--source",
        "change2",
        "--target",
        "change1",
        "--id",
        "sup_change2_change1",
    )
    assert result.exit_code == 0, result.output
    result = invoke(
        tmp_path,
        "state",
        "supersede",
        "sup_change2_change1",
    )
    assert result.exit_code == 0, result.output

    status = invoke(tmp_path, "gate", "show", "decision1")
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["freshness"] == "stale"
    assert "change1" in payload["stale_input_record_ids"]


@pytest.mark.parametrize(
    "record_type",
    ["VerificationReceipt", "PolicySnapshot", "GateRequest", "GateDecision"],
)
def test_generic_record_cli_cannot_impersonate_m04_semantic_records(
    tmp_path: Path,
    record_type: str,
) -> None:
    db = tmp_path / ".sayf" / "ledger.sqlite3"
    result = invoke(
        tmp_path,
        "record",
        "create",
        "--type",
        record_type,
        "--id",
        f"forged-{record_type}",
        "--payload",
        "{}",
    )

    assert result.exit_code != 0
    # Protection may happen in Typer's enum parsing or in RecordDraft's semantic
    # boundary depending on CLI/library versions. The authority invariant is that
    # generic creation never reaches an authoritative ledger append.
    assert not db.exists()
