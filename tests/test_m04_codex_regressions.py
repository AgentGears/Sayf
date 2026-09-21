from __future__ import annotations

from pathlib import Path

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind
from sayf.gates import GateRequestSpec, PolicySnapshotSpec, VerificationReceiptSpec
from sayf.records import (
    GateDecisionPayload,
    RecordDraft,
    RecordType,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
)

ACTOR = Actor(kind=ActorKind.HUMAN, id="m04-codex-regression")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m04-codex-verifier")
POLICY_ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="m04-codex-engine")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create(repo: CausalRepository, record_id: str, record_type: RecordType) -> None:
    repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type),
        actor=ACTOR,
    )


def record_pass(
    repo: CausalRepository,
    *,
    receipt_id: str,
    subject_id: str,
    contract: str,
    evidence_ids: tuple[str, ...],
) -> None:
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id=subject_id,
            contract=contract,
            result=VerificationResult.PASS,
            evidence_record_ids=evidence_ids,
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim=f"{contract} passed for the bound change",
        ),
        actor=VERIFIER,
        record_id=receipt_id,
    )


def test_gate_evaluated_inputs_list_all_receipts_before_evidence(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create(repo, "change1", RecordType.CHANGE_SET)
    create(repo, "evidence1", RecordType.EVIDENCE)
    create(repo, "evidence2", RecordType.EVIDENCE)
    create(repo, "evidence3", RecordType.EVIDENCE)

    record_pass(
        repo,
        receipt_id="receipt_tests",
        subject_id="change1",
        contract="tests",
        evidence_ids=("evidence1", "evidence2"),
    )
    record_pass(
        repo,
        receipt_id="receipt_lint",
        subject_id="change1",
        contract="lint",
        evidence_ids=("evidence2", "evidence3"),
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="policy",
            requirements=(
                VerificationRequirement(contract="tests"),
                VerificationRequirement(contract="lint"),
            ),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("receipt_tests", "receipt_lint"),
        ),
        actor=ACTOR,
        record_id="request1",
    )

    decision = repo.evaluate_gate(
        "request1",
        actor=POLICY_ENGINE,
        record_id="decision1",
    )
    payload = GateDecisionPayload.model_validate(decision.payload)

    assert [binding.record_id for binding in payload.evaluated_inputs] == [
        "request1",
        "change1",
        "policy1",
        "receipt_tests",
        "receipt_lint",
        "evidence1",
        "evidence2",
        "evidence3",
    ]
