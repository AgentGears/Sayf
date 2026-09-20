from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind
from sayf.gates import (
    GateProjectionError,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    GateOutcome,
    RecordDraft,
    RecordType,
    VerificationIndependence,
    VerificationReceiptPayload,
    VerificationRequirement,
    VerificationResult,
)

ACTOR = Actor(kind=ActorKind.HUMAN, id="m04-edge")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m04-verifier")
ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="m04-engine")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create(repo: CausalRepository, record_id: str, record_type: RecordType) -> None:
    repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type), actor=ACTOR
    )


def test_pass_requires_bounded_verified_claim() -> None:
    with pytest.raises(ValidationError, match="bounded verified_claim"):
        VerificationReceiptPayload(
            subject={"record_id": "subject", "content_hash": "sha256:" + "1" * 64},
            contract="tests",
            result=VerificationResult.PASS,
            evidence=(
                {"record_id": "evidence", "content_hash": "sha256:" + "2" * 64},
            ),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
        )


def test_fail_and_inconclusive_cannot_assert_verified_claim() -> None:
    for result in (VerificationResult.FAIL, VerificationResult.INCONCLUSIVE):
        with pytest.raises(ValidationError, match="must not assert"):
            VerificationReceiptPayload(
                subject={"record_id": "subject", "content_hash": "sha256:" + "1" * 64},
                contract="tests",
                result=result,
                evidence=(
                    {
                        "record_id": "evidence",
                        "content_hash": "sha256:" + "2" * 64,
                    },
                ),
                environment={"platform": "test"},
                independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
                verified_claim="unsupported claim",
            )


def test_policy_contract_names_must_be_unique_before_authoritative_append(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    before = len(repo.event_store.events())
    spec = PolicySnapshotSpec(
        name="duplicate",
        requirements=(
            VerificationRequirement(contract="tests"),
            VerificationRequirement(contract="tests", minimum_passes=2),
        ),
    )

    with pytest.raises(ValidationError, match="must be unique"):
        repo.register_policy_snapshot(spec, actor=ACTOR, record_id="policy1")
    assert len(repo.event_store.events()) == before


def test_gate_request_subject_is_change_set(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create(repo, "intent1", RecordType.INTENT_REVISION)
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="policy",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )

    with pytest.raises(GateProjectionError, match="must have type ChangeSet"):
        repo.request_gate(
            GateRequestSpec(
                subject_id="intent1",
                policy_snapshot_id="policy1",
            ),
            actor=ACTOR,
            record_id="request1",
        )


def test_minimum_passes_requires_distinct_receipts(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create(repo, "change1", RecordType.CHANGE_SET)
    create(repo, "evidence1", RecordType.EVIDENCE)
    create(repo, "evidence2", RecordType.EVIDENCE)

    for receipt_id, evidence_id in (
        ("receipt1", "evidence1"),
        ("receipt2", "evidence2"),
    ):
        repo.record_verification(
            VerificationReceiptSpec(
                subject_id="change1",
                contract="tests",
                result=VerificationResult.PASS,
                evidence_record_ids=(evidence_id,),
                environment={"platform": "test"},
                independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
                verified_claim="tests passed",
            ),
            actor=VERIFIER,
            record_id=receipt_id,
        )

    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="two-pass-policy",
            requirements=(
                VerificationRequirement(contract="tests", minimum_passes=2),
            ),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("receipt1", "receipt2"),
        ),
        actor=ACTOR,
        record_id="request1",
    )

    decision = repo.evaluate_gate("request1", actor=ENGINE, record_id="decision1")
    assert decision.payload["outcome"] == GateOutcome.PERMIT.value


def test_receipt_for_different_subject_cannot_be_bound_to_request(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create(repo, "change1", RecordType.CHANGE_SET)
    create(repo, "change2", RecordType.CHANGE_SET)
    create(repo, "evidence1", RecordType.EVIDENCE)
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change2",
            contract="tests",
            result=VerificationResult.PASS,
            evidence_record_ids=("evidence1",),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim="tests passed",
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="policy",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )

    with pytest.raises(GateProjectionError, match="verifies a different subject"):
        repo.request_gate(
            GateRequestSpec(
                subject_id="change1",
                policy_snapshot_id="policy1",
                verification_receipt_ids=("receipt1",),
            ),
            actor=ACTOR,
            record_id="request1",
        )
