from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.feedback import (
    ExplainEdgeKind,
    FeedbackApplicationState,
    FeedbackCaseSpec,
    M05ProjectionError,
    ReleaseAuthorityFreshness,
    ReleaseSpec,
    RuntimeObservationSpec,
)
from sayf.gates import (
    GateDecisionFreshness,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    FeedbackClassification,
    FeedbackEffect,
    GateOutcome,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    ReleasePayload,
    RuntimeOutcome,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
    semantic_record_event_payload,
)

ACTOR = Actor(kind=ActorKind.HUMAN, id="m05-test")
VERIFIER = Actor(kind=ActorKind.TOOL, id="test-runner")
POLICY_ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="gate-engine")
RUNTIME = Actor(kind=ActorKind.TOOL, id="runtime-monitor")


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


def permitted_change(repo: CausalRepository) -> None:
    create_record(repo, "assumption1", RecordType.ASSUMPTION)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    repo.create_relation(
        RelationDraft(
            relation_id="change_depends_assumption",
            relation_type=RelationType.DEPENDS_ON,
            source_id="change1",
            target_id="assumption1",
        ),
        actor=ACTOR,
    )
    repo.bind_dependency("change_depends_assumption", actor=ACTOR)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change1",
            contract="tests",
            result=VerificationResult.PASS,
            evidence_record_ids=("evidence1",),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.YES,
            verified_claim="tests passed for the bound change",
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="release-policy",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("receipt1",),
        ),
        actor=ACTOR,
        record_id="request1",
    )
    repo.evaluate_gate("request1", actor=POLICY_ENGINE, record_id="decision1")


def register_release(repo: CausalRepository) -> None:
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


def open_failed_feedback(repo: CausalRepository) -> None:
    create_record(repo, "runtime_evidence1", RecordType.EVIDENCE)
    repo.record_runtime_observation(
        RuntimeObservationSpec(
            release_id="release1",
            outcome=RuntimeOutcome.FAILED,
            summary="production behavior contradicts the bound assumption",
            environment={"stage": "production"},
            evidence_record_ids=("runtime_evidence1",),
        ),
        actor=RUNTIME,
        record_id="runtime1",
    )
    repo.open_feedback_case(
        FeedbackCaseSpec(
            observation_id="runtime1",
            target_id="assumption1",
            classification=FeedbackClassification.FALSIFIES,
            proposed_effect=FeedbackEffect.INVALIDATE_TARGET,
            rationale="runtime evidence falsifies assumption A1",
        ),
        actor=ACTOR,
        record_id="feedback1",
    )


def test_m05_semantic_types_require_dedicated_api_and_risk_remains_reserved() -> None:
    for record_type in (
        RecordType.RELEASE,
        RecordType.RUNTIME_OBSERVATION,
        RecordType.FEEDBACK_CASE,
    ):
        with pytest.raises(ValidationError, match="M0.5 semantic API"):
            RecordDraft(record_type=record_type, payload={})
    with pytest.raises(ValidationError, match="reserved"):
        RecordDraft(record_type=RecordType.RISK_ASSESSMENT, payload={})


def test_release_requires_fresh_permit_and_duplicate_ref_is_rejected(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)

    status = repo.release_status("release1")
    assert status.authority_freshness is ReleaseAuthorityFreshness.FRESH
    assert status.stale_authority_record_ids == ()

    with pytest.raises(M05ProjectionError, match="already used"):
        repo.register_release(
            ReleaseSpec(
                release_ref="R1",
                subject_id="change1",
                gate_decision_id="decision1",
                environment={"stage": "production-2"},
            ),
            actor=ACTOR,
            record_id="release_duplicate",
        )


def test_feedback_case_is_not_authority_until_explicitly_applied(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)
    open_failed_feedback(repo)

    before = repo.effective_state().state("assumption1")
    assert before.validity.value == "not_invalidated"
    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.NOT_APPLIED
    )

    applied = repo.apply_feedback("feedback1", actor=ACTOR)
    assert applied.state is FeedbackApplicationState.APPLIED
    assert applied.invalidation_event_id is not None
    assert repo.effective_state().state("assumption1").validity.value == "invalidated"

    second = repo.apply_feedback("feedback1", actor=ACTOR)
    assert second == applied


def test_runtime_feedback_closes_loop_and_stales_gate_and_release(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)
    open_failed_feedback(repo)

    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH
    assert (
        repo.release_status("release1").authority_freshness
        is ReleaseAuthorityFreshness.FRESH
    )

    repo.apply_feedback("feedback1", actor=ACTOR)

    change_state = repo.effective_state().state("change1")
    assert change_state.freshness.value == "stale"
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.STALE
    release = repo.release_status("release1")
    assert release.authority_freshness is ReleaseAuthorityFreshness.STALE
    assert "change1" in release.stale_authority_record_ids
    assert "decision1" in release.stale_authority_record_ids


def test_why_and_impact_expose_feedback_and_authority_paths(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)
    open_failed_feedback(repo)
    repo.apply_feedback("feedback1", actor=ACTOR)

    why = repo.why("assumption1", max_depth=8, max_results=100)
    by_end = {path.end_id: path for path in why}
    assert "feedback1" in by_end
    assert by_end["feedback1"].steps[0].kind is ExplainEdgeKind.INVALIDATION_CAUSE
    assert "runtime1" in by_end
    assert "release1" in by_end

    impact = repo.impact("assumption1", max_depth=8, max_results=100)
    impacted = {path.end_id for path in impact}
    assert "change1" in impacted
    assert "decision1" in impacted
    assert "release1" in impacted


def test_timeline_includes_dependency_feedback_and_invalidation(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)
    open_failed_feedback(repo)
    repo.apply_feedback("feedback1", actor=ACTOR)

    timeline = repo.timeline("assumption1")
    roles = {role for entry in timeline for role in entry.roles}
    assert "record_created" in roles
    assert "relation_target:depends_on" in roles
    assert any(role.startswith("state_target:sayf.dependency.bound") for role in roles)
    assert "bound_by:feedback_target" in roles
    assert "relation_target:invalidates" in roles
    assert any(role.startswith("state_target:sayf.record.invalidated") for role in roles)


def test_unrelated_event_does_not_stale_release(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)

    repo.event_store.append(
        EventDraft(
            stream_id="external",
            event_type="external.note.v1",
            actor=ACTOR,
            payload={"note": "unrelated"},
        )
    )

    assert (
        repo.release_status("release1").authority_freshness
        is ReleaseAuthorityFreshness.FRESH
    )


def test_privileged_release_without_valid_authority_fails_closed(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change1",
            contract="tests",
            result=VerificationResult.FAIL,
            evidence_record_ids=("evidence1",),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.YES,
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="release-policy",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("receipt1",),
        ),
        actor=ACTOR,
        record_id="request1",
    )
    decision = repo.evaluate_gate(
        "request1", actor=POLICY_ENGINE, record_id="decision1"
    )
    assert repo.gate_status(decision.id).outcome is GateOutcome.BLOCK

    graph = repo.graph()
    subject = graph.record("change1")
    gate = graph.record("decision1")
    forged = ReleasePayload(
        release_ref="FORGED",
        subject={"record_id": subject.id, "content_hash": subject.content_hash},
        gate_decision={"record_id": gate.id, "content_hash": gate.content_hash},
        authority_fingerprint_version=1,
        authority_fingerprint="sha256:" + "0" * 64,
        environment={"stage": "production"},
    )
    repo.event_store.append(
        EventDraft(
            event_id="release_forged",
            stream_id="record:release_forged",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=ACTOR,
            payload=semantic_record_event_payload(RecordType.RELEASE, forged),
        )
    )

    with pytest.raises(M05ProjectionError, match="not permit"):
        repo.feedback()
    with pytest.raises(M05ProjectionError, match="not permit"):
        create_record(repo, "blocked_after_forgery", RecordType.CLAIM)


def test_explainability_bounds_fail_instead_of_silently_truncating(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_change(repo)
    register_release(repo)

    with pytest.raises(M05ProjectionError, match="result bound"):
        repo.impact("change1", max_depth=8, max_results=1)
