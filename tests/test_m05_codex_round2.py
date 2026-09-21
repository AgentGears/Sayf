from __future__ import annotations

from pathlib import Path

import pytest

import sayf.causal as causal_module
from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.feedback import (
    FeedbackApplicationState,
    FeedbackCaseSpec,
    M05ProjectionError,
    ReleaseSpec,
    RuntimeObservationSpec,
    feedback_invalidation_relation_draft,
)
from sayf.gates import (
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    FeedbackClassification,
    FeedbackEffect,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    RuntimeOutcome,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
)
from sayf.state import RECORD_INVALIDATED_EVENT_TYPE, semantic_relation_event_payload

ACTOR = Actor(kind=ActorKind.HUMAN, id="m05-codex-round2")
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


def make_pending_feedback(repo: CausalRepository) -> str:
    case = repo.graph().record("feedback1")
    relation = repo.create_relation(feedback_invalidation_relation_draft(case), actor=ACTOR)
    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.PENDING_QUALIFICATION
    )
    return relation.id


def prepare_runtime_evidence_invalidation(repo: CausalRepository) -> str:
    create_record(repo, "runtime_evidence_invalidator", RecordType.OBSERVATION)
    relation = repo.create_relation(
        RelationDraft(
            relation_id="invalidate_runtime_evidence",
            relation_type=RelationType.INVALIDATES,
            source_id="runtime_evidence_invalidator",
            target_id="runtime_evidence1",
        ),
        actor=ACTOR,
    )
    return relation.id


def test_generic_invalidate_cannot_bypass_managed_feedback_authority(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)
    relation_id = make_pending_feedback(repo)

    with pytest.raises(M05ProjectionError, match="qualified through apply_feedback"):
        repo.invalidate(relation_id, actor=ACTOR)

    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.PENDING_QUALIFICATION
    )
    assert repo.effective_state().state("assumption1").validity.value == "not_invalidated"


def test_privileged_ineligible_feedback_qualification_fails_replay_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)
    relation_id = make_pending_feedback(repo)
    source_invalidation_id = prepare_runtime_evidence_invalidation(repo)
    repo.invalidate(source_invalidation_id, actor=ACTOR)

    relation = repo.graph().relation(relation_id)
    repo.event_store.append(
        EventDraft(
            stream_id=f"state:{relation.id}",
            event_type=RECORD_INVALIDATED_EVENT_TYPE,
            actor=ACTOR,
            payload=semantic_relation_event_payload(relation),
        )
    )

    with pytest.raises(M05ProjectionError, match="source records are not usable"):
        repo.feedback()
    with pytest.raises(M05ProjectionError, match="source records are not usable"):
        create_record(repo, "blocked_after_forged_feedback", RecordType.CLAIM)


def test_source_change_racing_qualification_forces_revalidation_and_blocks_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)
    relation_id = make_pending_feedback(repo)
    source_invalidation_id = prepare_runtime_evidence_invalidation(repo)

    original_append = causal_module.append_if_ledger_head
    injected = False

    def racing_append(store, draft, *, expected_event_count, expected_head_hash):
        nonlocal injected
        if (
            not injected
            and draft.event_type == RECORD_INVALIDATED_EVENT_TYPE
            and draft.stream_id == f"state:{relation_id}"
        ):
            injected = True
            repo.invalidate(source_invalidation_id, actor=ACTOR)
        return original_append(
            store,
            draft,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    monkeypatch.setattr(causal_module, "append_if_ledger_head", racing_append)

    with pytest.raises(M05ProjectionError, match="source records are not usable"):
        repo.apply_feedback("feedback1", actor=ACTOR)

    assert injected is True
    assert (
        repo.feedback_application_status("feedback1").state
        is FeedbackApplicationState.PENDING_QUALIFICATION
    )
    assert repo.effective_state().state("assumption1").validity.value == "not_invalidated"


def test_unrelated_head_movement_during_qualification_retries_and_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)
    relation_id = make_pending_feedback(repo)

    original_append = causal_module.append_if_ledger_head
    injected = False

    def racing_append(store, draft, *, expected_event_count, expected_head_hash):
        nonlocal injected
        if (
            not injected
            and draft.event_type == RECORD_INVALIDATED_EVENT_TYPE
            and draft.stream_id == f"state:{relation_id}"
        ):
            injected = True
            repo.event_store.append(
                EventDraft(
                    stream_id="external",
                    event_type="external.concurrent-note.v1",
                    actor=ACTOR,
                    payload={"kind": "unrelated"},
                )
            )
        return original_append(
            store,
            draft,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    monkeypatch.setattr(causal_module, "append_if_ledger_head", racing_append)

    status = repo.apply_feedback("feedback1", actor=ACTOR)

    assert injected is True
    assert status.state is FeedbackApplicationState.APPLIED
    assert repo.effective_state().state("assumption1").validity.value == "invalidated"


def test_unrelated_head_movement_during_relation_creation_retries_and_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(tmp_path)
    permitted_release(repo)
    open_feedback(repo)

    relation_id = feedback_invalidation_relation_draft(
        repo.graph().record("feedback1")
    ).relation_id
    original_append = causal_module.append_if_ledger_head
    injected = False

    def racing_append(store, draft, *, expected_event_count, expected_head_hash):
        nonlocal injected
        if (
            not injected
            and draft.event_type == "sayf.relation.created.v1"
            and draft.event_id == relation_id
        ):
            injected = True
            repo.event_store.append(
                EventDraft(
                    stream_id="external",
                    event_type="external.concurrent-note.v1",
                    actor=ACTOR,
                    payload={"kind": "unrelated"},
                )
            )
        return original_append(
            store,
            draft,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    monkeypatch.setattr(causal_module, "append_if_ledger_head", racing_append)

    status = repo.apply_feedback("feedback1", actor=ACTOR)

    assert injected is True
    assert status.state is FeedbackApplicationState.APPLIED
    assert repo.effective_state().state("assumption1").validity.value == "invalidated"
