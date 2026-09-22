from __future__ import annotations

import json
from pathlib import Path

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


def create(root: Path, record_type: str, record_id: str) -> None:
    result = invoke(
        root,
        "record",
        "create",
        "--type",
        record_type,
        "--id",
        record_id,
    )
    assert result.exit_code == 0, result.output


def test_cli_risk_assess_and_show_returns_record_and_effective_state(
    tmp_path: Path,
) -> None:
    create(tmp_path, "ChangeSet", "change1")
    create(tmp_path, "Evidence", "evidence1")

    assessed = invoke(
        tmp_path,
        "risk",
        "assess",
        "--id",
        "risk1",
        "--payload",
        json.dumps(
            {
                "subject_id": "change1",
                "method": "bounded-change-risk-v1",
                "findings": [
                    {
                        "finding_id": "risk-1",
                        "category": "operability",
                        "statement": "rollback may be required",
                        "severity": "high",
                        "likelihood": "possible",
                        "mitigation": "retain rollback procedure",
                        "residual_severity": "moderate",
                    }
                ],
                "overall_conclusion": "acceptable",
                "evidence_record_ids": ["evidence1"],
                "environment": {"stage": "pre-release"},
                "limitations": ["bounded to this change"],
                "prohibited_generalizations": ["acceptable does not mean zero risk"],
            }
        ),
    )
    assert assessed.exit_code == 0, assessed.output
    record = json.loads(assessed.output)
    assert record["type"] == "RiskAssessment"
    assert record["payload"]["subject"]["record_id"] == "change1"
    assert record["payload"]["overall_conclusion"] == "acceptable"

    shown = invoke(tmp_path, "risk", "show", "risk1")
    assert shown.exit_code == 0, shown.output
    view = json.loads(shown.output)
    assert view["record"]["id"] == "risk1"
    assert view["record"]["type"] == "RiskAssessment"
    assert view["effective_state"]["validity"] == "not_invalidated"
    assert view["effective_state"]["revision"] == "current"
    assert view["effective_state"]["freshness"] == "fresh"


def test_generic_record_cli_cannot_impersonate_risk_assessment(tmp_path: Path) -> None:
    db = tmp_path / ".sayf" / "ledger.sqlite3"
    result = invoke(
        tmp_path,
        "record",
        "create",
        "--type",
        "RiskAssessment",
        "--id",
        "forged-risk",
        "--payload",
        "{}",
    )

    assert result.exit_code != 0
    assert not db.exists()


def test_cli_risk_rejects_wrong_subject_without_appending(tmp_path: Path) -> None:
    create(tmp_path, "Assumption", "assumption1")
    result = invoke(
        tmp_path,
        "risk",
        "assess",
        "--id",
        "risk1",
        "--payload",
        json.dumps(
            {
                "subject_id": "assumption1",
                "method": "bounded-change-risk-v1",
                "overall_conclusion": "inconclusive",
                "environment": {"stage": "pre-release"},
            }
        ),
    )

    assert result.exit_code == 1
    assert "must have type ChangeSet" in result.output

    shown = invoke(tmp_path, "record", "show", "risk1")
    assert shown.exit_code == 1
