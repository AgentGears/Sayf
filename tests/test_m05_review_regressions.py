from __future__ import annotations

from pathlib import Path

import pytest

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind
from sayf.feedback import (
    ExplainEdgeKind,
    FeedbackApplicationState,
    FeedbackCaseSpec,
    M05ProjectionError,
    ReleaseAuthorityFreshness,
    ReleaseSpec,
    RuntimeObservationSpec,
    feedback_invalidation_relation_draft,
    release_authority_fingerprint,
)
from sayf.gates import (
    GateDecisionFreshness,
    GateDecisionStatus,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    FeedbackClassification,
    FeedbackEffect,
    GateOutcome,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    RuntimeOutcome,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
)
from sayf.storage import LedgerReadError

ACTOR = Actor(kind=ActorKind.HUMAN, id="m05-review")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m05-verifier")
POLICY_ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="m05-policy")
RUNTIME = Actor(kind=ActorKind.TOOL, id="m05-runtime")


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


def permitted_release(repo: CausalRepository) -> None:
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
            verified_claim="tests passed for exact change1",
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


def open_feedback(repo: CausalRepository) -> None:
    create_record(repo, "runtime_evidence1", RecordType.EVIDENCE)
    repo.record_runtime_observation(
        RuntimeObservationSpec(
            release_id="release1",
            outcome=RuntimeOutcome.FAILED,
            summary="runtime result contradicts assumption1",
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
            rationale="runtime evidence falsifies assumption1",
        ),
        actor=ACTOR,
        record_id="feedback1",
    )


def invalidate_record(repo: CausalRepository, target_id: str, suffix: str) -> None:
    source_id = f"invalidator_{suffix}"
    relation_id = f"invalidate_{suffix}"
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


def test_explainability_preserves_alternate_paths_to_same_endpoint(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)

    paths = repo.why("release1", max_depth=4, max_results=100, max_expansions=1000)
    change_paths = [path for path in paths if path.end_id == "change1"]

    assert len(change_paths) >= 2
    step_kinds = {tuple(step.kind for step in path.steps) for path in change_paths}
    assert (ExplainEdgeKind.RELEASE_SUBJECT,) in step_kinds
    assert (
        ExplainEdgeKind.RELEASE_GATE,
        ExplainEdgeKind.GATE_EVALUATED_INPUT,
    ) in step_kinds


def test_explainability_expansion_bound_fails_closed(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)

    with pytest.raises(M05ProjectionError, match="expansion bound"):
        repo.why(
            "release1",
            max_depth=8,
            max_results=100,
            max_expansions=1,
        )


@pytest.mark.parametrize(
    ("target_id", "suffix"),
    [
        ("feedback1", "case"),
        ("runtime1", "observation"),
        ("runtime_evidence1", "evidence"),
    ],
)
def test_unusable_feedback_sources_cannot_create_new_authority(
    tmp_path: Path,
    target_id: str,
    suffix: str,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)
    invalidate_record(repo, target_id, suffix)

    with pytest.raises(M05ProjectionError, match="source records are not usable"):
        repo.apply_feedback("feedback1", actor=ACTOR)

    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.NOT_APPLIED
    )


def test_pending_feedback_relation_is_not_qualified_after_source_becomes_unusable(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)

    case = repo.graph().record("feedback1")
    repo.create_relation(feedback_invalidation_relation_draft(case), actor=ACTOR)
    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.PENDING_QUALIFICATION
    )

    invalidate_record(repo, "runtime_evidence1", "pending_evidence")

    with pytest.raises(M05ProjectionError, match="source records are not usable"):
        repo.apply_feedback("feedback1", actor=ACTOR)

    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.PENDING_QUALIFICATION
    )
    assert repo.effective_state().state("assumption1").validity.value == "not_invalidated"


def test_feedback_application_recovers_compatible_concurrent_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)

    original_create_relation = repo.create_relation
    original_invalidate = repo.invalidate
    create_injected = False
    invalidate_injected = False

    def competing_create(draft: RelationDraft, *, actor: Actor):
        nonlocal create_injected
        if not create_injected:
            create_injected = True
            original_create_relation(draft, actor=actor)
            raise LedgerReadError("simulated competing relation commit")
        return original_create_relation(draft, actor=actor)

    def competing_invalidate(relation_id: str, *, actor: Actor):
        nonlocal invalidate_injected
        if not invalidate_injected:
            invalidate_injected = True
            original_invalidate(relation_id, actor=actor)
            raise LedgerReadError("simulated competing qualification commit")
        return original_invalidate(relation_id, actor=actor)

    monkeypatch.setattr(repo, "create_relation", competing_create)
    monkeypatch.setattr(repo, "invalidate", competing_invalidate)

    status = repo.apply_feedback("feedback1", actor=ACTOR)

    assert create_injected is True
    assert invalidate_injected is True
    assert status.state is FeedbackApplicationState.APPLIED
    assert repo.effective_state().state("assumption1").validity.value == "invalidated"


def test_release_authority_fingerprint_is_minimal_and_versioned(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)

    release = repo.graph().record("release1")
    assert release.payload["authority_fingerprint_version"] == 1

    status = repo.gate_status("decision1")
    same_authority_extra_context = GateDecisionStatus(
        decision_id=status.decision_id,
        gate_request_id="different-context-does-not-change-v1-authority",
        outcome=status.outcome,
        freshness=status.freshness,
        stale_input_record_ids=("context-only",),
        reasons=("context-only",),
        satisfied_requirements=("context-only",),
        missing_requirements=("context-only",),
    )
    assert release_authority_fingerprint(status) == release_authority_fingerprint(
        same_authority_extra_context
    )

    stale = status.model_copy(update={"freshness": GateDecisionFreshness.STALE})
    assert release_authority_fingerprint(status) != release_authority_fingerprint(stale)

    release_status = repo.release_status("release1")
    dumped = release_status.model_dump(mode="json")
    assert dumped["authority_freshness"] == ReleaseAuthorityFreshness.FRESH.value
    assert "freshness" not in dumped


def test_applied_feedback_remains_historical_after_source_is_later_invalidated(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)
    applied = repo.apply_feedback("feedback1", actor=ACTOR)
    assert applied.state is FeedbackApplicationState.APPLIED

    invalidate_record(repo, "runtime1", "after_application")

    repeated = repo.apply_feedback("feedback1", actor=ACTOR)
    assert repeated.state is FeedbackApplicationState.APPLIED
    assert repeated.invalidation_event_id == applied.invalidation_event_id


def test_release_authority_stales_when_bound_gate_stales(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)

    before = repo.release_status("release1")
    assert before.authority_freshness is ReleaseAuthorityFreshness.FRESH

    invalidate_record(repo, "evidence1", "gate_evidence")

    gate = repo.gate_status("decision1")
    release = repo.release_status("release1")
    assert gate.outcome is GateOutcome.PERMIT
    assert gate.freshness is GateDecisionFreshness.STALE
    assert release.authority_freshness is ReleaseAuthorityFreshness.STALE
    assert "evidence1" in release.stale_authority_record_ids
    assert "decision1" in release.stale_authority_record_ids
