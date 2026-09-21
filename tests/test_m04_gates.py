from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import sayf.causal as causal_module
from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.gates import (
    GateDecisionFreshness,
    GateProjectionError,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    GateDecisionPayload,
    GateOutcome,
    RecordBinding,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    VerificationIndependence,
    VerificationReceiptPayload,
    VerificationRequirement,
    VerificationResult,
    record_content_hash,
    record_event_payload,
    semantic_record_event_payload,
)
from sayf.storage import LedgerReadError, SQLiteEventStore

ACTOR = Actor(kind=ActorKind.HUMAN, id="m04-test")
VERIFIER = Actor(kind=ActorKind.TOOL, id="test-runner")
POLICY_ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="gate-engine")


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


def passing_receipt(
    repo: CausalRepository,
    *,
    record_id: str,
    subject_id: str,
    evidence_id: str,
    contract: str = "tests",
) -> None:
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id=subject_id,
            contract=contract,
            result=VerificationResult.PASS,
            evidence_record_ids=(evidence_id,),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim=f"{contract} passed for the bound change",
            prohibited_generalizations=("does not establish release safety",),
        ),
        actor=VERIFIER,
        record_id=record_id,
    )


def policy(
    repo: CausalRepository,
    *,
    record_id: str,
    contracts: tuple[str, ...],
) -> None:
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="test-policy",
            requirements=tuple(
                VerificationRequirement(contract=contract) for contract in contracts
            ),
        ),
        actor=ACTOR,
        record_id=record_id,
    )


def gate_request(
    repo: CausalRepository,
    *,
    record_id: str,
    subject_id: str,
    policy_id: str,
    receipt_ids: tuple[str, ...],
) -> None:
    repo.request_gate(
        GateRequestSpec(
            subject_id=subject_id,
            policy_snapshot_id=policy_id,
            verification_receipt_ids=receipt_ids,
        ),
        actor=ACTOR,
        record_id=record_id,
    )


def test_gate_permit_binds_exact_evidence_policy_and_effective_state(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )

    decision = repo.evaluate_gate(
        "request1",
        actor=POLICY_ENGINE,
        record_id="decision1",
    )
    payload = GateDecisionPayload.model_validate(decision.payload)
    assert payload.outcome is GateOutcome.PERMIT
    assert payload.reasons == ()
    assert payload.satisfied_requirements == ("tests",)
    assert payload.missing_requirements == ()
    assert [binding.record_id for binding in payload.evaluated_inputs] == [
        "request1",
        "change1",
        "policy1",
        "receipt1",
        "evidence1",
    ]

    status = repo.gate_status("decision1")
    assert status.outcome is GateOutcome.PERMIT
    assert status.freshness is GateDecisionFreshness.FRESH
    assert status.stale_input_record_ids == ()


def test_missing_required_verification_blocks_gate(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
        contract="tests",
    )
    policy(repo, record_id="policy1", contracts=("tests", "lint"))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )

    decision = repo.evaluate_gate(
        "request1", actor=POLICY_ENGINE, record_id="decision1"
    )
    payload = GateDecisionPayload.model_validate(decision.payload)
    assert payload.outcome is GateOutcome.BLOCK
    assert payload.satisfied_requirements == ("tests",)
    assert payload.missing_requirements == ("lint",)
    assert "requirement_unsatisfied:lint:0/1" in payload.reasons
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH


def test_non_pass_receipt_blocks_even_if_policy_contract_matches(tmp_path: Path) -> None:
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
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim=None,
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )

    payload = GateDecisionPayload.model_validate(
        repo.evaluate_gate(
            "request1", actor=POLICY_ENGINE, record_id="decision1"
        ).payload
    )
    assert payload.outcome is GateOutcome.BLOCK
    assert "receipt_not_pass:receipt1:fail" in payload.reasons
    assert "requirement_unsatisfied:tests:0/1" in payload.reasons


def test_block_decision_can_be_fresh_when_input_was_already_invalidated(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    create_record(repo, "obs1", RecordType.OBSERVATION)
    repo.create_relation(
        RelationDraft(
            relation_id="invalidate_evidence",
            relation_type=RelationType.INVALIDATES,
            source_id="obs1",
            target_id="evidence1",
        ),
        actor=ACTOR,
    )
    repo.invalidate("invalidate_evidence", actor=ACTOR)

    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )

    decision = repo.evaluate_gate(
        "request1", actor=POLICY_ENGINE, record_id="decision1"
    )
    payload = GateDecisionPayload.model_validate(decision.payload)
    assert payload.outcome is GateOutcome.BLOCK
    assert any(reason.startswith("input_not_usable:evidence1:") for reason in payload.reasons)
    status = repo.gate_status("decision1")
    assert status.freshness is GateDecisionFreshness.FRESH


def test_permit_becomes_stale_when_bound_subject_is_superseded(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )
    repo.evaluate_gate("request1", actor=POLICY_ENGINE, record_id="decision1")

    create_record(repo, "change2", RecordType.CHANGE_SET)
    repo.create_relation(
        RelationDraft(
            relation_id="sup_change2_change1",
            relation_type=RelationType.SUPERSEDES,
            source_id="change2",
            target_id="change1",
        ),
        actor=ACTOR,
    )
    repo.supersede("sup_change2_change1", actor=ACTOR)

    status = repo.gate_status("decision1")
    assert status.outcome is GateOutcome.PERMIT
    assert status.freshness is GateDecisionFreshness.STALE
    assert "change1" in status.stale_input_record_ids


def test_unrelated_ledger_event_does_not_stale_gate_decision(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )
    repo.evaluate_gate("request1", actor=POLICY_ENGINE, record_id="decision1")

    repo.event_store.append(
        EventDraft(
            stream_id="external",
            event_type="external.note.v1",
            actor=ACTOR,
            payload={"note": "unrelated"},
        )
    )

    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH


def test_m04_semantic_record_types_cannot_use_generic_record_surface() -> None:
    with pytest.raises(ValidationError, match="M0.4 semantic API"):
        RecordDraft(record_type=RecordType.VERIFICATION_RECEIPT, payload={})
    with pytest.raises(ValidationError, match="M0.4 semantic API"):
        RecordDraft(record_type=RecordType.POLICY_SNAPSHOT, payload={})
    with pytest.raises(ValidationError, match="M0.4 semantic API"):
        RecordDraft(record_type=RecordType.GATE_REQUEST, payload={})
    with pytest.raises(ValidationError, match="M0.4 semantic API"):
        RecordDraft(record_type=RecordType.GATE_DECISION, payload={})


def test_privileged_forward_verification_binding_fails_projection_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    future = RecordDraft(record_id="future_change", record_type=RecordType.CHANGE_SET)
    future_hash = record_content_hash(future)
    evidence = repo.graph().record("evidence1")

    malformed = VerificationReceiptPayload(
        subject=RecordBinding(record_id="future_change", content_hash=future_hash),
        contract="tests",
        result=VerificationResult.PASS,
        evidence=(
            RecordBinding(record_id=evidence.id, content_hash=evidence.content_hash),
        ),
        environment={"platform": "test"},
        independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
        verified_claim="tests passed",
    )
    repo.event_store.append(
        EventDraft(
            event_id="receipt_bad",
            stream_id="record:receipt_bad",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=VERIFIER,
            payload=semantic_record_event_payload(
                RecordType.VERIFICATION_RECEIPT, malformed
            ),
        )
    )
    repo.event_store.append(
        EventDraft(
            event_id="future_change",
            stream_id="record:future_change",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=ACTOR,
            payload=record_event_payload(future),
        )
    )

    with pytest.raises(GateProjectionError, match="must exist before"):
        repo.gates()
    with pytest.raises(GateProjectionError, match="must exist before"):
        create_record(repo, "blocked", RecordType.CLAIM)


def test_privileged_forged_permit_is_rejected_by_historical_replay(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests", "lint"))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )

    expected = repo.gates().evaluate_request("request1")
    assert expected.outcome is GateOutcome.BLOCK
    forged = expected.model_copy(
        update={
            "outcome": GateOutcome.PERMIT,
            "reasons": (),
            "missing_requirements": (),
        }
    )
    forged = GateDecisionPayload.model_validate(forged.model_dump(mode="python"))
    repo.event_store.append(
        EventDraft(
            event_id="decision_forged",
            stream_id="record:decision_forged",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=POLICY_ENGINE,
            payload=semantic_record_event_payload(RecordType.GATE_DECISION, forged),
        )
    )

    with pytest.raises(GateProjectionError, match="does not match deterministic evaluation"):
        repo.gates()


def test_second_decision_for_same_request_fails_projection_closed(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )
    decision = repo.evaluate_gate(
        "request1", actor=POLICY_ENGINE, record_id="decision1"
    )
    payload = GateDecisionPayload.model_validate(decision.payload)

    repo.event_store.append(
        EventDraft(
            event_id="decision2",
            stream_id="record:decision2",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=POLICY_ENGINE,
            payload=semantic_record_event_payload(RecordType.GATE_DECISION, payload),
        )
    )

    with pytest.raises(GateProjectionError, match="already has decision"):
        repo.gates()


def test_gate_evaluation_refuses_head_change_after_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)
    passing_receipt(
        repo,
        record_id="receipt1",
        subject_id="change1",
        evidence_id="evidence1",
    )
    policy(repo, record_id="policy1", contracts=("tests",))
    gate_request(
        repo,
        record_id="request1",
        subject_id="change1",
        policy_id="policy1",
        receipt_ids=("receipt1",),
    )

    original_append = causal_module.append_if_ledger_head
    injected = False

    def inject_concurrent_event(store, draft, **precondition):
        nonlocal injected
        if not injected:
            injected = True
            SQLiteEventStore(store.path).append(
                EventDraft(
                    stream_id="external",
                    event_type="external.note.v1",
                    actor=ACTOR,
                    payload={"concurrent": True},
                )
            )
        return original_append(store, draft, **precondition)

    monkeypatch.setattr(causal_module, "append_if_ledger_head", inject_concurrent_event)

    with pytest.raises(LedgerReadError, match="ledger head changed"):
        repo.evaluate_gate("request1", actor=POLICY_ENGINE, record_id="decision1")

    assert repo.gates().decisions == ()
