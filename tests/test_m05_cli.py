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


def prepare_permitted_change(root: Path) -> None:
    create(root, "Assumption", "assumption1")
    create(root, "ChangeSet", "change1")
    result = invoke(
        root,
        "relation",
        "create",
        "--type",
        "depends_on",
        "--source",
        "change1",
        "--target",
        "assumption1",
        "--id",
        "change_depends_assumption",
    )
    assert result.exit_code == 0, result.output
    result = invoke(root, "state", "bind-dependency", "change_depends_assumption")
    assert result.exit_code == 0, result.output

    create(root, "Evidence", "evidence1")
    result = invoke(
        root,
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
                "verified_claim": "tests passed for the exact change",
            }
        ),
    )
    assert result.exit_code == 0, result.output
    result = invoke(
        root,
        "policy",
        "register",
        "--id",
        "policy1",
        "--payload",
        json.dumps(
            {
                "name": "release-policy",
                "requirements": [{"contract": "tests", "minimum_passes": 1}],
            }
        ),
    )
    assert result.exit_code == 0, result.output
    result = invoke(
        root,
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
    assert result.exit_code == 0, result.output
    result = invoke(
        root,
        "gate",
        "evaluate",
        "request1",
        "--id",
        "decision1",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["payload"]["outcome"] == "permit"


def test_cli_m05_release_feedback_and_explainability_loop(tmp_path: Path) -> None:
    prepare_permitted_change(tmp_path)

    release = invoke(
        tmp_path,
        "release",
        "register",
        "--id",
        "release1",
        "--payload",
        json.dumps(
            {
                "release_ref": "R1",
                "subject_id": "change1",
                "gate_decision_id": "decision1",
                "environment": {"stage": "production"},
            }
        ),
    )
    assert release.exit_code == 0, release.output
    release_payload = json.loads(release.output)["payload"]
    assert release_payload["authority_fingerprint_version"] == 1

    shown = invoke(tmp_path, "release", "show", "release1")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["authority_freshness"] == "fresh"

    create(tmp_path, "Evidence", "runtime_evidence1")
    runtime = invoke(
        tmp_path,
        "runtime",
        "record",
        "--id",
        "runtime1",
        "--payload",
        json.dumps(
            {
                "release_id": "release1",
                "outcome": "failed",
                "summary": "runtime behavior falsifies assumption1",
                "environment": {"stage": "production"},
                "evidence_record_ids": ["runtime_evidence1"],
            }
        ),
    )
    assert runtime.exit_code == 0, runtime.output

    feedback = invoke(
        tmp_path,
        "feedback",
        "open",
        "--id",
        "feedback1",
        "--payload",
        json.dumps(
            {
                "observation_id": "runtime1",
                "target_id": "assumption1",
                "classification": "falsifies",
                "proposed_effect": "invalidate_target",
                "rationale": "production evidence contradicts assumption1",
            }
        ),
    )
    assert feedback.exit_code == 0, feedback.output

    shown_feedback = invoke(tmp_path, "feedback", "show", "feedback1")
    assert shown_feedback.exit_code == 0, shown_feedback.output
    assert json.loads(shown_feedback.output)["state"] == "not_applied"

    applied = invoke(tmp_path, "feedback", "apply", "feedback1")
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["state"] == "applied"

    release_after = invoke(tmp_path, "release", "show", "release1")
    assert release_after.exit_code == 0, release_after.output
    assert json.loads(release_after.output)["authority_freshness"] == "stale"

    why = invoke(
        tmp_path,
        "explain",
        "why",
        "release1",
        "--max-depth",
        "4",
        "--max-results",
        "100",
        "--max-expansions",
        "1000",
    )
    assert why.exit_code == 0, why.output
    paths = json.loads(why.output)
    assert any(path["end_id"] == "change1" for path in paths)

    impact = invoke(
        tmp_path,
        "explain",
        "impact",
        "assumption1",
        "--max-depth",
        "8",
        "--max-results",
        "100",
        "--max-expansions",
        "1000",
    )
    assert impact.exit_code == 0, impact.output
    impacted = {path["end_id"] for path in json.loads(impact.output)}
    assert "release1" in impacted

    timeline = invoke(tmp_path, "explain", "timeline", "assumption1")
    assert timeline.exit_code == 0, timeline.output
    assert any(
        entry["event_type"] == "sayf.record.invalidated.v1"
        for entry in json.loads(timeline.output)
    )


def test_cli_explainability_expansion_bound_fails_closed(tmp_path: Path) -> None:
    prepare_permitted_change(tmp_path)
    release = invoke(
        tmp_path,
        "release",
        "register",
        "--id",
        "release1",
        "--payload",
        json.dumps(
            {
                "release_ref": "R1",
                "subject_id": "change1",
                "gate_decision_id": "decision1",
                "environment": {"stage": "production"},
            }
        ),
    )
    assert release.exit_code == 0, release.output

    result = invoke(
        tmp_path,
        "explain",
        "why",
        "release1",
        "--max-expansions",
        "1",
    )
    assert result.exit_code == 1
    assert "expansion bound" in result.output


@pytest.mark.parametrize("record_type", ["Release", "RuntimeObservation", "FeedbackCase"])
def test_generic_record_cli_cannot_impersonate_m05_semantic_records(
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
    assert not db.exists()
