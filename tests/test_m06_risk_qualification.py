from __future__ import annotations

from pathlib import Path

import pytest

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind
from sayf.feedback import ReleaseAuthorityFreshness, ReleaseSpec
from sayf.gates import (
    GateDecisionFreshness,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.graph import GraphProjectionError
from sayf.records import (
    GateOutcome,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    RiskConclusion,
    RiskFinding,
    RiskLikelihood,
    RiskSeverity,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
)
from sayf.risk import RiskAssessmentSpec, RiskProjectionError

ACTOR = Actor(kind=ActorKind.HUMAN, id="m06-review")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m06-verifier")
POLICY_ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="m06-policy")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create_record(
    repo: CausalRepository,
    record_id: str,
    record_type: RecordType,
) -> None:
    repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type),
        actor=ACTOR,
    )


def risk_finding(finding_id: str = "risk-1") -> RiskFinding:
    return RiskFinding(
        finding_id=finding_id,
        category="operability",
        statement="deployment may require rollback under partial dependency failure",
        severity=RiskSeverity.HIGH,
        likelihood=RiskLikelihood.POSSIBLE,
        mitigation="retain rollback procedure and verification evidence",
        residual_severity=RiskSeverity.MODERATE,
    )


def create_risk_assessment(repo: CausalRepository, *, record_id: str = "risk1") -> None:
    repo.record_risk_assessment(
        RiskAssessmentSpec(
            subject_id="change1",
            method="bounded-change-risk-v1",
            findings=(risk_finding(),),
            overall_conclusion=RiskConclusion.ACCEPTABLE,
            evidence_record_ids=("risk_evidence1",),
            environment={"stage": "pre-release"},
            assumptions=("rollback remains available",),
            limitations=("does not assess organizational or regulatory risk",),
            prohibited_generalizations=("acceptable does not mean zero risk",),
        ),
        actor=ACTOR,
        record_id=record_id,
    )


def risk_receipt_spec(
    *,
    subject_id: str = "change1",
    evidence_record_ids: tuple[str, ...] = ("risk1", "risk_evidence1"),
) -> VerificationReceiptSpec:
    return VerificationReceiptSpec(
        subject_id=subject_id,
        contract="risk:qualified",
        result=VerificationResult.PASS,
        evidence_record_ids=evidence_record_ids,
        environment={"stage": "pre-release"},
        independent_from_generation=VerificationIndependence.YES,
        verified_claim=(
            "the bound risk assessment satisfies the bounded risk qualification "
            "contract for change1"
        ),
    )


def qualify_and_release(repo: CausalRepository) -> None:
    repo.record_verification(
        risk_receipt_spec(),
        actor=VERIFIER,
        record_id="risk_receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="risk-aware-release-policy",
            requirements=(VerificationRequirement(contract="risk:qualified"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("risk_receipt1",),
        ),
        actor=ACTOR,
        record_id="request1",
    )
    decision = repo.evaluate_gate(
        "request1",
        actor=POLICY_ENGINE,
        record_id="decision1",
    )
    assert decision.payload["outcome"] == GateOutcome.PERMIT.value
    repo.register_release(
        ReleaseSpec(
            release_ref="R1",
            subject_id="change1",
            gate_decision_id="decision1",
            environment={"stage": "production"},
        ),
        actor=ACTOR,
        record_id="release1",
    )


def invalidate_record(repo: CausalRepository, target_id: str, suffix: str) -> None:
    source_id = f"risk_observation_{suffix}"
    relation_id = f"risk_invalidated_{suffix}"
    create_record(repo, source_id, RecordType.OBSERVATION)
    repo.create_relation(
        RelationDraft(
            relation_id=relation_id,
            relation_type=RelationType.INVALIDATES,
            source_id=source_id,
            target_id=target_id,
        ),
        actor=ACTOR,
    )
    repo.invalidate(relation_id, actor=ACTOR)


def test_risk_qualification_composes_with_existing_gate_and_release_authority(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)
    create_risk_assessment(repo)
    qualify_and_release(repo)

    assessment = repo.risk_assessment("risk1")
    assert assessment.subject.record_id == "change1"
    assert assessment.overall_conclusion is RiskConclusion.ACCEPTABLE
    assert assessment.evidence[0].record_id == "risk_evidence1"
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH
    assert (
        repo.release_status("release1").authority_freshness
        is ReleaseAuthorityFreshness.FRESH
    )

    invalidate_record(repo, "risk1", "assessment")

    gate_status = repo.gate_status("decision1")
    assert gate_status.freshness is GateDecisionFreshness.STALE
    assert "risk1" in gate_status.stale_input_record_ids
    release_status = repo.release_status("release1")
    assert release_status.authority_freshness is ReleaseAuthorityFreshness.STALE
    assert "risk1" in release_status.stale_authority_record_ids


def test_risk_underlying_evidence_change_stales_gate_and_release(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)
    create_risk_assessment(repo)
    qualify_and_release(repo)

    invalidate_record(repo, "risk_evidence1", "evidence")

    gate_status = repo.gate_status("decision1")
    assert gate_status.freshness is GateDecisionFreshness.STALE
    assert "risk_evidence1" in gate_status.stale_input_record_ids
    release_status = repo.release_status("release1")
    assert release_status.authority_freshness is ReleaseAuthorityFreshness.STALE
    assert "risk_evidence1" in release_status.stale_authority_record_ids


def test_favorable_assessment_does_not_satisfy_policy_without_qualification_receipt(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)
    create_risk_assessment(repo)
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="risk-aware-release-policy",
            requirements=(VerificationRequirement(contract="risk:qualified"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=(),
        ),
        actor=ACTOR,
        record_id="request1",
    )

    decision = repo.evaluate_gate(
        "request1",
        actor=POLICY_ENGINE,
        record_id="decision1",
    )

    assert decision.payload["outcome"] == GateOutcome.BLOCK.value
    assert "risk:qualified" in decision.payload["missing_requirements"]


def test_risk_qualification_requires_same_subject_before_append(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "change2", RecordType.CHANGE_SET)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)
    create_risk_assessment(repo)

    with pytest.raises(RiskProjectionError, match="different ChangeSet subject"):
        repo.record_verification(
            risk_receipt_spec(subject_id="change2"),
            actor=VERIFIER,
            record_id="bad_receipt",
        )

    with pytest.raises(GraphProjectionError):
        repo.graph().record("bad_receipt")


def test_risk_qualification_requires_evidence_closure_before_append(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)
    create_risk_assessment(repo)

    with pytest.raises(RiskProjectionError, match="without binding its exact evidence"):
        repo.record_verification(
            risk_receipt_spec(evidence_record_ids=("risk1",)),
            actor=VERIFIER,
            record_id="bad_receipt",
        )

    with pytest.raises(GraphProjectionError):
        repo.graph().record("bad_receipt")


def test_generic_record_surface_cannot_impersonate_risk_assessment() -> None:
    with pytest.raises(ValueError, match="M0.6 semantic API"):
        RecordDraft(
            record_id="risk1",
            record_type=RecordType.RISK_ASSESSMENT,
            payload={},
        )


def test_risk_assessment_requires_changeset_subject(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "not_change", RecordType.ASSUMPTION)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)

    with pytest.raises(RiskProjectionError, match="must have type ChangeSet"):
        repo.record_risk_assessment(
            RiskAssessmentSpec(
                subject_id="not_change",
                method="bounded-change-risk-v1",
                findings=(),
                overall_conclusion=RiskConclusion.INCONCLUSIVE,
                evidence_record_ids=("risk_evidence1",),
                environment={"stage": "pre-release"},
            ),
            actor=ACTOR,
            record_id="risk1",
        )

    with pytest.raises(GraphProjectionError):
        repo.graph().record("risk1")


def test_duplicate_findings_fail_before_append(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "risk_evidence1", RecordType.EVIDENCE)

    with pytest.raises(ValueError, match="risk finding ids must be unique"):
        repo.record_risk_assessment(
            RiskAssessmentSpec(
                subject_id="change1",
                method="bounded-change-risk-v1",
                findings=(risk_finding("same"), risk_finding("same")),
                overall_conclusion=RiskConclusion.ATTENTION_REQUIRED,
                evidence_record_ids=("risk_evidence1",),
                environment={"stage": "pre-release"},
            ),
            actor=ACTOR,
            record_id="risk1",
        )

    with pytest.raises(GraphProjectionError):
        repo.graph().record("risk1")
