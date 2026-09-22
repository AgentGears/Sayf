from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sayf.domain import LedgerEvent, _normalize_json_object, _normalize_text
from sayf.feedback import M05Projection
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.records import (
    Record,
    RecordBinding,
    RecordType,
    RiskAssessmentPayload,
    RiskConclusion,
    RiskFinding,
    VerificationReceiptPayload,
)


class RiskProjectionError(GraphProjectionError):
    """Raised when M0.6 risk-assessment history cannot be projected safely."""


class RiskAssessmentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    findings: tuple[RiskFinding, ...] = ()
    overall_conclusion: RiskConclusion
    evidence_record_ids: tuple[str, ...] = ()
    environment: dict[str, Any] = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    prohibited_generalizations: tuple[str, ...] = ()

    @field_validator("subject_id", "method")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_text(value, "risk assessment specification text")

    @field_validator("evidence_record_ids")
    @classmethod
    def normalize_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _normalize_text(item, "risk assessment evidence record id") for item in value
        )
        if any(not item for item in normalized):
            raise ValueError("risk assessment evidence record ids must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("risk assessment evidence record ids must be unique")
        return normalized

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "risk assessment environment")

    @field_validator("assumptions", "limitations", "prohibited_generalizations")
    @classmethod
    def normalize_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_normalize_text(item, "risk assessment text") for item in value)


def record_binding(record: Record) -> RecordBinding:
    return RecordBinding(record_id=record.id, content_hash=record.content_hash)


def _resolve_binding(
    graph: CausalGraph,
    binding: RecordBinding,
    *,
    expected_type: RecordType | None = None,
    before_sequence: int | None = None,
    role: str,
) -> Record:
    try:
        record = graph.record(binding.record_id)
    except GraphProjectionError as exc:
        raise RiskProjectionError(
            f"{role} references missing record {binding.record_id}"
        ) from exc
    if record.content_hash != binding.content_hash:
        raise RiskProjectionError(
            f"{role} binding for {record.id} does not match immutable content hash"
        )
    if expected_type is not None and record.type is not expected_type:
        raise RiskProjectionError(
            f"{role} record {record.id} must have type {expected_type.value}, "
            f"found {record.type.value}"
        )
    if before_sequence is not None and record.created_sequence >= before_sequence:
        raise RiskProjectionError(
            f"{role} record {record.id} must exist before the record that binds it"
        )
    return record


def _risk_payload(record: Record) -> RiskAssessmentPayload:
    try:
        return RiskAssessmentPayload.model_validate(record.payload)
    except ValidationError as exc:
        raise RiskProjectionError(
            f"risk assessment {record.id} has invalid payload: {exc}"
        ) from exc


def build_risk_assessment_payload(
    graph: CausalGraph,
    spec: RiskAssessmentSpec,
) -> RiskAssessmentPayload:
    subject = graph.record(spec.subject_id)
    if subject.type is not RecordType.CHANGE_SET:
        raise RiskProjectionError(
            f"risk assessment subject {subject.id} must have type ChangeSet, "
            f"found {subject.type.value}"
        )
    evidence = tuple(graph.record(record_id) for record_id in spec.evidence_record_ids)
    return RiskAssessmentPayload(
        subject=record_binding(subject),
        method=spec.method,
        findings=spec.findings,
        overall_conclusion=spec.overall_conclusion,
        evidence=tuple(record_binding(record) for record in evidence),
        environment=spec.environment,
        assumptions=spec.assumptions,
        limitations=spec.limitations,
        prohibited_generalizations=spec.prohibited_generalizations,
    )


def validate_verification_risk_composition(
    graph: CausalGraph,
    payload: VerificationReceiptPayload,
    *,
    receipt_id: str,
) -> None:
    """Keep RiskAssessment qualification bound to its subject and evidence closure."""
    evidence_by_id = {binding.record_id: binding for binding in payload.evidence}
    for binding in payload.evidence:
        evidence_record = graph.record(binding.record_id)
        if evidence_record.type is not RecordType.RISK_ASSESSMENT:
            continue
        assessment = _risk_payload(evidence_record)
        if payload.subject != assessment.subject:
            raise RiskProjectionError(
                f"verification receipt {receipt_id} uses risk assessment "
                f"{evidence_record.id} for a different ChangeSet subject"
            )
        for required_binding in assessment.evidence:
            actual_binding = evidence_by_id.get(required_binding.record_id)
            if actual_binding != required_binding:
                raise RiskProjectionError(
                    f"verification receipt {receipt_id} uses risk assessment "
                    f"{evidence_record.id} without binding its exact evidence record "
                    f"{required_binding.record_id}"
                )


class M06Projection:
    """M0.6 semantic projection layered over fully validated M0.5 history."""

    def __init__(
        self,
        *,
        events: tuple[LedgerEvent, ...],
        feedback: M05Projection,
    ) -> None:
        self._events = events
        self._feedback = feedback
        self._graph = feedback.graph

    @classmethod
    def from_events(cls, events: Iterable[LedgerEvent]) -> M06Projection:
        event_list = tuple(events)
        feedback = M05Projection.from_events(event_list)
        graph = feedback.graph

        for record in graph.records:
            if record.type is not RecordType.RISK_ASSESSMENT:
                continue
            try:
                payload = _risk_payload(record)
                _resolve_binding(
                    graph,
                    payload.subject,
                    expected_type=RecordType.CHANGE_SET,
                    before_sequence=record.created_sequence,
                    role=f"risk assessment {record.id} subject",
                )
                for binding in payload.evidence:
                    _resolve_binding(
                        graph,
                        binding,
                        before_sequence=record.created_sequence,
                        role=f"risk assessment {record.id} evidence",
                    )
            except RiskProjectionError:
                raise
            except (ValidationError, GraphProjectionError, ValueError) as exc:
                raise RiskProjectionError(
                    f"invalid M0.6 semantic record at ledger sequence "
                    f"{record.created_sequence} ({record.id}): {exc}"
                ) from exc

        for record in graph.records:
            if record.type is not RecordType.VERIFICATION_RECEIPT:
                continue
            try:
                receipt = VerificationReceiptPayload.model_validate(record.payload)
                validate_verification_risk_composition(
                    graph,
                    receipt,
                    receipt_id=record.id,
                )
            except RiskProjectionError:
                raise
            except (ValidationError, GraphProjectionError, ValueError) as exc:
                raise RiskProjectionError(
                    f"invalid M0.6 risk qualification at ledger sequence "
                    f"{record.created_sequence} ({record.id}): {exc}"
                ) from exc

        return cls(events=event_list, feedback=feedback)

    @property
    def graph(self) -> CausalGraph:
        return self._graph

    @property
    def feedback_projection(self) -> M05Projection:
        return self._feedback

    @property
    def state_projection(self):
        return self._feedback.state_projection

    @property
    def gate_projection(self):
        return self._feedback.gate_projection

    def risk_assessment(self, record_id: str) -> RiskAssessmentPayload:
        record = self._graph.record(record_id)
        if record.type is not RecordType.RISK_ASSESSMENT:
            raise RiskProjectionError(f"record {record_id} is not a RiskAssessment")
        return _risk_payload(record).model_copy(deep=True)
