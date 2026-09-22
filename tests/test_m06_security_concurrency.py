from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.gates import VerificationReceiptSpec
from sayf.graph import GraphProjectionError
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    Record,
    RecordBinding,
    RecordDraft,
    RecordType,
    RiskAssessmentPayload,
    RiskConclusion,
    VerificationIndependence,
    VerificationReceiptPayload,
    VerificationResult,
    record_event_payload,
    semantic_record_event_payload,
)
from sayf.risk import RiskAssessmentSpec, RiskProjectionError
from sayf.storage import LedgerReadError

ACTOR = Actor(kind=ActorKind.HUMAN, id="m06-security")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m06-verifier")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create_record(
    repo: CausalRepository,
    record_id: str,
    record_type: RecordType,
) -> Record:
    return repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type),
        actor=ACTOR,
    )


def binding(record: Record) -> RecordBinding:
    return RecordBinding(record_id=record.id, content_hash=record.content_hash)


def append_privileged_risk(
    repo: CausalRepository,
    *,
    record_id: str,
    subject: RecordBinding,
    evidence: tuple[RecordBinding, ...] = (),
) -> None:
    payload = RiskAssessmentPayload(
        subject=subject,
        method="privileged-test",
        overall_conclusion=RiskConclusion.INCONCLUSIVE,
        evidence=evidence,
        environment={"stage": "test"},
    )
    repo.event_store.append(
        EventDraft(
            event_id=record_id,
            stream_id=f"record:{record_id}",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=ACTOR,
            payload=semantic_record_event_payload(RecordType.RISK_ASSESSMENT, payload),
        )
    )


def test_privileged_risk_with_wrong_subject_type_fails_semantic_facade_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    assumption = create_record(repo, "assumption1", RecordType.ASSUMPTION)
    append_privileged_risk(repo, record_id="risk_forged", subject=binding(assumption))

    with pytest.raises(RiskProjectionError, match="must have type ChangeSet"):
        repo.risk()
    with pytest.raises(RiskProjectionError, match="must have type ChangeSet"):
        create_record(repo, "blocked_after_forgery", RecordType.CLAIM)


def test_privileged_risk_forward_evidence_binding_fails_closed(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    change = create_record(repo, "change1", RecordType.CHANGE_SET)
    future_draft = RecordDraft(record_id="future_evidence", record_type=RecordType.EVIDENCE)
    future_binding = RecordBinding(
        record_id=future_draft.record_id,
        content_hash=(
            record_event_payload(future_draft)["content_hash"]
        ),
    )
    append_privileged_risk(
        repo,
        record_id="risk_forged",
        subject=binding(change),
        evidence=(future_binding,),
    )
    repo.event_store.append(
        EventDraft(
            event_id=future_draft.record_id,
            stream_id=f"record:{future_draft.record_id}",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=ACTOR,
            payload=record_event_payload(future_draft),
        )
    )

    with pytest.raises(RiskProjectionError, match="must exist before"):
        repo.graph()


def test_privileged_receipt_missing_risk_evidence_closure_fails_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    change = create_record(repo, "change1", RecordType.CHANGE_SET)
    evidence = create_record(repo, "risk_evidence1", RecordType.EVIDENCE)
    risk = repo.record_risk_assessment(
        RiskAssessmentSpec(
            subject_id=change.id,
            method="bounded-risk-v1",
            overall_conclusion=RiskConclusion.ACCEPTABLE,
            evidence_record_ids=(evidence.id,),
            environment={"stage": "test"},
        ),
        actor=ACTOR,
        record_id="risk1",
    )
    payload = VerificationReceiptPayload(
        subject=binding(change),
        contract="risk:qualified",
        result=VerificationResult.PASS,
        evidence=(binding(risk),),
        environment={"stage": "test"},
        independent_from_generation=VerificationIndependence.YES,
        verified_claim="the bound risk assessment passed the bounded contract",
    )
    repo.event_store.append(
        EventDraft(
            event_id="receipt_forged",
            stream_id="record:receipt_forged",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=VERIFIER,
            payload=semantic_record_event_payload(
                RecordType.VERIFICATION_RECEIPT,
                payload,
            ),
        )
    )

    with pytest.raises(RiskProjectionError, match="without binding its exact evidence"):
        repo.gates()


def test_supported_risk_write_rejects_exact_head_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    original_append = repo._append_semantic_record
    raced = False

    def racing_append(*args: Any, **kwargs: Any) -> Record:
        nonlocal raced
        if not raced:
            raced = True
            repo.event_store.append(
                EventDraft(
                    stream_id="external",
                    event_type="external.concurrent.v1",
                    actor=ACTOR,
                    payload={"source": "race"},
                )
            )
        return original_append(*args, **kwargs)

    monkeypatch.setattr(repo, "_append_semantic_record", racing_append)

    with pytest.raises(LedgerReadError, match="ledger head changed"):
        repo.record_risk_assessment(
            RiskAssessmentSpec(
                subject_id="change1",
                method="bounded-risk-v1",
                overall_conclusion=RiskConclusion.INCONCLUSIVE,
                environment={"stage": "test"},
            ),
            actor=ACTOR,
            record_id="risk_raced",
        )

    with pytest.raises(GraphProjectionError):
        repo.graph().record("risk_raced")


def test_non_risk_verification_contract_is_unchanged(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "change1", RecordType.CHANGE_SET)
    create_record(repo, "evidence1", RecordType.EVIDENCE)

    receipt = repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change1",
            contract="tests",
            result=VerificationResult.PASS,
            evidence_record_ids=("evidence1",),
            environment={"stage": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim="tests passed for the exact change",
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )

    assert receipt.type is RecordType.VERIFICATION_RECEIPT
