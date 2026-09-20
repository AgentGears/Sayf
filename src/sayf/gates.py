from __future__ import annotations

import hashlib
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sayf.domain import LedgerEvent, _normalize_json_object, _normalize_text
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.hashing import HASH_PREFIX, canonical_json
from sayf.records import (
    GateDecisionPayload,
    GateInputBinding,
    GateOutcome,
    GateRequestPayload,
    PolicySnapshotPayload,
    Record,
    RecordBinding,
    RecordType,
    VerificationIndependence,
    VerificationReceiptPayload,
    VerificationRequirement,
    VerificationResult,
)
from sayf.state import (
    EffectiveStateProjection,
    FreshnessState,
    RecordEffectiveState,
    RevisionState,
    ValidityState,
)


class GateProjectionError(GraphProjectionError):
    """Raised when M0.4 evidence/gate history cannot be projected safely."""


class GateDecisionFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class VerificationReceiptSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    result: VerificationResult
    evidence_record_ids: tuple[str, ...] = Field(min_length=1)
    environment: dict[str, Any] = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    independent_from_generation: VerificationIndependence
    verified_claim: str | None = None
    prohibited_generalizations: tuple[str, ...] = ()

    @field_validator("subject_id", "contract")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_text(value, "verification specification text")

    @field_validator("evidence_record_ids")
    @classmethod
    def normalize_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _normalize_text(item, "verification evidence record id") for item in value
        )
        if any(not item for item in normalized):
            raise ValueError("verification evidence record ids must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("verification evidence record ids must be unique")
        return normalized

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "verification environment")


class PolicySnapshotSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    requirements: tuple[VerificationRequirement, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_text(value, "policy snapshot name")


class GateRequestSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str = Field(min_length=1)
    policy_snapshot_id: str = Field(min_length=1)
    verification_receipt_ids: tuple[str, ...] = ()

    @field_validator("subject_id", "policy_snapshot_id")
    @classmethod
    def normalize_required_id(cls, value: str) -> str:
        return _normalize_text(value, "gate request record id")

    @field_validator("verification_receipt_ids")
    @classmethod
    def normalize_receipt_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _normalize_text(item, "gate request receipt id") for item in value
        )
        if any(not item for item in normalized):
            raise ValueError("gate request receipt ids must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("gate request receipt ids must be unique")
        return normalized


class GateDecisionStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    gate_request_id: str
    outcome: GateOutcome
    freshness: GateDecisionFreshness
    stale_input_record_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    satisfied_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()


def _sha256(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def record_binding(record: Record) -> RecordBinding:
    return RecordBinding(record_id=record.id, content_hash=record.content_hash)


def effective_state_hash(state: RecordEffectiveState) -> str:
    return _sha256(state.model_dump(mode="json"))


def gate_input_binding(
    record: Record,
    state: RecordEffectiveState,
) -> GateInputBinding:
    return GateInputBinding(
        record_id=record.id,
        content_hash=record.content_hash,
        effective_state_hash=effective_state_hash(state),
    )


def _is_usable(state: RecordEffectiveState) -> bool:
    return (
        state.validity is ValidityState.NOT_INVALIDATED
        and state.revision is RevisionState.CURRENT
        and state.freshness is FreshnessState.FRESH
    )


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
        raise GateProjectionError(
            f"{role} references missing record {binding.record_id}"
        ) from exc
    if record.content_hash != binding.content_hash:
        raise GateProjectionError(
            f"{role} binding for {record.id} does not match immutable content hash"
        )
    if expected_type is not None and record.type is not expected_type:
        raise GateProjectionError(
            f"{role} record {record.id} must have type {expected_type.value}, "
            f"found {record.type.value}"
        )
    if before_sequence is not None and record.created_sequence >= before_sequence:
        raise GateProjectionError(
            f"{role} record {record.id} must exist before the record that binds it"
        )
    return record


def _receipt_payload(record: Record) -> VerificationReceiptPayload:
    try:
        return VerificationReceiptPayload.model_validate(record.payload)
    except ValidationError as exc:
        raise GateProjectionError(
            f"verification receipt {record.id} has invalid payload: {exc}"
        ) from exc


def _policy_payload(record: Record) -> PolicySnapshotPayload:
    try:
        return PolicySnapshotPayload.model_validate(record.payload)
    except ValidationError as exc:
        raise GateProjectionError(
            f"policy snapshot {record.id} has invalid payload: {exc}"
        ) from exc


def _request_payload(record: Record) -> GateRequestPayload:
    try:
        return GateRequestPayload.model_validate(record.payload)
    except ValidationError as exc:
        raise GateProjectionError(
            f"gate request {record.id} has invalid payload: {exc}"
        ) from exc


def _decision_payload(record: Record) -> GateDecisionPayload:
    try:
        return GateDecisionPayload.model_validate(record.payload)
    except ValidationError as exc:
        raise GateProjectionError(
            f"gate decision {record.id} has invalid payload: {exc}"
        ) from exc


def _validate_verification_record(graph: CausalGraph, record: Record) -> None:
    payload = _receipt_payload(record)
    _resolve_binding(
        graph,
        payload.subject,
        before_sequence=record.created_sequence,
        role=f"verification receipt {record.id} subject",
    )
    for binding in payload.evidence:
        _resolve_binding(
            graph,
            binding,
            before_sequence=record.created_sequence,
            role=f"verification receipt {record.id} evidence",
        )


def _validate_policy_record(record: Record) -> None:
    _policy_payload(record)


def _validate_request_record(graph: CausalGraph, record: Record) -> None:
    payload = _request_payload(record)
    _resolve_binding(
        graph,
        payload.subject,
        expected_type=RecordType.CHANGE_SET,
        before_sequence=record.created_sequence,
        role=f"gate request {record.id} subject",
    )
    _resolve_binding(
        graph,
        payload.policy_snapshot,
        expected_type=RecordType.POLICY_SNAPSHOT,
        before_sequence=record.created_sequence,
        role=f"gate request {record.id} policy",
    )
    for binding in payload.verification_receipts:
        receipt = _resolve_binding(
            graph,
            binding,
            expected_type=RecordType.VERIFICATION_RECEIPT,
            before_sequence=record.created_sequence,
            role=f"gate request {record.id} verification receipt",
        )
        receipt_payload = _receipt_payload(receipt)
        if receipt_payload.subject != payload.subject:
            raise GateProjectionError(
                f"gate request {record.id} receipt {receipt.id} verifies a different subject"
            )


def build_verification_receipt_payload(
    graph: CausalGraph,
    spec: VerificationReceiptSpec,
) -> VerificationReceiptPayload:
    subject = graph.record(spec.subject_id)
    evidence = tuple(graph.record(record_id) for record_id in spec.evidence_record_ids)
    return VerificationReceiptPayload(
        subject=record_binding(subject),
        contract=spec.contract,
        result=spec.result,
        evidence=tuple(record_binding(record) for record in evidence),
        environment=spec.environment,
        limitations=spec.limitations,
        independent_from_generation=spec.independent_from_generation,
        verified_claim=spec.verified_claim,
        prohibited_generalizations=spec.prohibited_generalizations,
    )


def build_policy_snapshot_payload(spec: PolicySnapshotSpec) -> PolicySnapshotPayload:
    return PolicySnapshotPayload(name=spec.name, requirements=spec.requirements)


def build_gate_request_payload(
    graph: CausalGraph,
    spec: GateRequestSpec,
) -> GateRequestPayload:
    subject = graph.record(spec.subject_id)
    if subject.type is not RecordType.CHANGE_SET:
        raise GateProjectionError(
            f"gate request subject {subject.id} must have type ChangeSet, "
            f"found {subject.type.value}"
        )
    policy = graph.record(spec.policy_snapshot_id)
    if policy.type is not RecordType.POLICY_SNAPSHOT:
        raise GateProjectionError(
            f"gate request policy {policy.id} must have type PolicySnapshot, "
            f"found {policy.type.value}"
        )

    receipts = tuple(graph.record(record_id) for record_id in spec.verification_receipt_ids)
    subject_binding = record_binding(subject)
    for receipt in receipts:
        if receipt.type is not RecordType.VERIFICATION_RECEIPT:
            raise GateProjectionError(
                f"gate request receipt {receipt.id} must have type VerificationReceipt, "
                f"found {receipt.type.value}"
            )
        if _receipt_payload(receipt).subject != subject_binding:
            raise GateProjectionError(
                f"gate request receipt {receipt.id} verifies a different subject"
            )

    return GateRequestPayload(
        subject=subject_binding,
        policy_snapshot=record_binding(policy),
        verification_receipts=tuple(record_binding(record) for record in receipts),
    )


def _evaluation_input_records(
    graph: CausalGraph,
    request_record: Record,
    request: GateRequestPayload,
) -> tuple[Record, ...]:
    ordered: list[Record] = [
        request_record,
        _resolve_binding(
            graph,
            request.subject,
            expected_type=RecordType.CHANGE_SET,
            before_sequence=request_record.created_sequence,
            role=f"gate request {request_record.id} subject",
        ),
        _resolve_binding(
            graph,
            request.policy_snapshot,
            expected_type=RecordType.POLICY_SNAPSHOT,
            before_sequence=request_record.created_sequence,
            role=f"gate request {request_record.id} policy",
        ),
    ]
    seen = {record.id for record in ordered}

    for binding in request.verification_receipts:
        receipt = _resolve_binding(
            graph,
            binding,
            expected_type=RecordType.VERIFICATION_RECEIPT,
            before_sequence=request_record.created_sequence,
            role=f"gate request {request_record.id} receipt",
        )
        if receipt.id not in seen:
            ordered.append(receipt)
            seen.add(receipt.id)
        for evidence_binding in _receipt_payload(receipt).evidence:
            evidence = _resolve_binding(
                graph,
                evidence_binding,
                before_sequence=receipt.created_sequence,
                role=f"verification receipt {receipt.id} evidence",
            )
            if evidence.id not in seen:
                ordered.append(evidence)
                seen.add(evidence.id)
    return tuple(ordered)


def evaluate_gate_request(
    graph: CausalGraph,
    state: EffectiveStateProjection,
    request_record: Record,
) -> GateDecisionPayload:
    if request_record.type is not RecordType.GATE_REQUEST:
        raise GateProjectionError(
            f"record {request_record.id} is not a GateRequest"
        )
    _validate_request_record(graph, request_record)
    request = _request_payload(request_record)
    policy_record = _resolve_binding(
        graph,
        request.policy_snapshot,
        expected_type=RecordType.POLICY_SNAPSHOT,
        before_sequence=request_record.created_sequence,
        role=f"gate request {request_record.id} policy",
    )
    policy = _policy_payload(policy_record)

    input_records = _evaluation_input_records(graph, request_record, request)
    state_by_id = {record.id: state.state(record.id) for record in input_records}
    reasons: list[str] = []

    for record in input_records:
        record_state = state_by_id[record.id]
        if not _is_usable(record_state):
            reasons.append(
                "input_not_usable:"
                f"{record.id}:{record_state.validity.value}:"
                f"{record_state.revision.value}:{record_state.freshness.value}"
            )

    receipts: list[tuple[Record, VerificationReceiptPayload]] = []
    for binding in request.verification_receipts:
        receipt = _resolve_binding(
            graph,
            binding,
            expected_type=RecordType.VERIFICATION_RECEIPT,
            before_sequence=request_record.created_sequence,
            role=f"gate request {request_record.id} receipt",
        )
        payload = _receipt_payload(receipt)
        receipts.append((receipt, payload))
        if payload.result is not VerificationResult.PASS:
            reasons.append(
                f"receipt_not_pass:{receipt.id}:{payload.result.value}"
            )

    satisfied: list[str] = []
    missing: list[str] = []
    for requirement in policy.requirements:
        eligible_count = 0
        for receipt, payload in receipts:
            if payload.contract != requirement.contract:
                continue
            if payload.result is not VerificationResult.PASS:
                continue
            if not _is_usable(state_by_id[receipt.id]):
                continue
            evidence_usable = all(
                _is_usable(state_by_id[binding.record_id])
                for binding in payload.evidence
            )
            if evidence_usable:
                eligible_count += 1
        if eligible_count >= requirement.minimum_passes:
            satisfied.append(requirement.contract)
        else:
            missing.append(requirement.contract)
            reasons.append(
                "requirement_unsatisfied:"
                f"{requirement.contract}:{eligible_count}/{requirement.minimum_passes}"
            )

    outcome = GateOutcome.PERMIT if not reasons else GateOutcome.BLOCK
    return GateDecisionPayload(
        gate_request=record_binding(request_record),
        subject=request.subject,
        policy_snapshot=request.policy_snapshot,
        verification_receipts=request.verification_receipts,
        evaluated_inputs=tuple(
            gate_input_binding(record, state_by_id[record.id]) for record in input_records
        ),
        outcome=outcome,
        reasons=tuple(reasons),
        satisfied_requirements=tuple(satisfied),
        missing_requirements=tuple(missing),
    )


class GateProjection:
    """Derived M0.4 evidence and deterministic gate state over immutable history."""

    def __init__(
        self,
        *,
        state_projection: EffectiveStateProjection,
        decisions_by_id: dict[str, GateDecisionStatus],
        decision_id_by_request: dict[str, str],
    ) -> None:
        self._state_projection = state_projection
        self._graph = state_projection.graph
        self._decisions_by_id = decisions_by_id
        self._decision_id_by_request = decision_id_by_request

    @classmethod
    def from_events(cls, events: Iterable[LedgerEvent]) -> GateProjection:
        event_list = tuple(events)
        state = EffectiveStateProjection.from_events(event_list)
        graph = state.graph
        decision_records: list[Record] = []
        decision_id_by_request: dict[str, str] = {}

        for record in sorted(graph.records, key=lambda item: item.created_sequence):
            try:
                if record.type is RecordType.VERIFICATION_RECEIPT:
                    _validate_verification_record(graph, record)
                elif record.type is RecordType.POLICY_SNAPSHOT:
                    _validate_policy_record(record)
                elif record.type is RecordType.GATE_REQUEST:
                    _validate_request_record(graph, record)
                elif record.type is RecordType.GATE_DECISION:
                    actual = _decision_payload(record)
                    if actual.gate_request.record_id in decision_id_by_request:
                        existing = decision_id_by_request[actual.gate_request.record_id]
                        raise GateProjectionError(
                            f"gate request {actual.gate_request.record_id} already has "
                            f"decision {existing}"
                        )
                    prefix = event_list[: record.created_sequence - 1]
                    prefix_state = EffectiveStateProjection.from_events(prefix)
                    request_record = _resolve_binding(
                        prefix_state.graph,
                        actual.gate_request,
                        expected_type=RecordType.GATE_REQUEST,
                        before_sequence=record.created_sequence,
                        role=f"gate decision {record.id} request",
                    )
                    expected = evaluate_gate_request(
                        prefix_state.graph,
                        prefix_state,
                        request_record,
                    )
                    if actual != expected:
                        raise GateProjectionError(
                            f"gate decision {record.id} does not match deterministic "
                            "evaluation of its bound request at decision time"
                        )
                    decision_id_by_request[actual.gate_request.record_id] = record.id
                    decision_records.append(record)
            except GateProjectionError:
                raise
            except (ValidationError, GraphProjectionError, ValueError) as exc:
                raise GateProjectionError(
                    f"invalid M0.4 semantic record at ledger sequence "
                    f"{record.created_sequence} ({record.id}): {exc}"
                ) from exc

        decisions_by_id: dict[str, GateDecisionStatus] = {}
        for record in decision_records:
            payload = _decision_payload(record)
            stale_input_ids: list[str] = []

            decision_state = state.state(record.id)
            if not _is_usable(decision_state):
                stale_input_ids.append(record.id)

            for expected_input in payload.evaluated_inputs:
                current_record = graph.record(expected_input.record_id)
                current_state = state.state(expected_input.record_id)
                if (
                    current_record.content_hash != expected_input.content_hash
                    or effective_state_hash(current_state)
                    != expected_input.effective_state_hash
                ):
                    stale_input_ids.append(expected_input.record_id)

            decisions_by_id[record.id] = GateDecisionStatus(
                decision_id=record.id,
                gate_request_id=payload.gate_request.record_id,
                outcome=payload.outcome,
                freshness=(
                    GateDecisionFreshness.STALE
                    if stale_input_ids
                    else GateDecisionFreshness.FRESH
                ),
                stale_input_record_ids=tuple(stale_input_ids),
                reasons=payload.reasons,
                satisfied_requirements=payload.satisfied_requirements,
                missing_requirements=payload.missing_requirements,
            )

        return cls(
            state_projection=state,
            decisions_by_id=decisions_by_id,
            decision_id_by_request=decision_id_by_request,
        )

    @property
    def graph(self) -> CausalGraph:
        return self._graph

    @property
    def state_projection(self) -> EffectiveStateProjection:
        return self._state_projection

    @property
    def decisions(self) -> tuple[GateDecisionStatus, ...]:
        return tuple(
            self._decisions_by_id[record.id]
            for record in self._graph.records
            if record.id in self._decisions_by_id
        )

    def status(self, decision_id: str) -> GateDecisionStatus:
        record = self._graph.record(decision_id)
        if record.type is not RecordType.GATE_DECISION:
            raise GateProjectionError(f"record {decision_id} is not a GateDecision")
        return self._decisions_by_id[decision_id]

    def evaluate_request(self, request_id: str) -> GateDecisionPayload:
        request = self._graph.record(request_id)
        if request.type is not RecordType.GATE_REQUEST:
            raise GateProjectionError(f"record {request_id} is not a GateRequest")
        if request_id in self._decision_id_by_request:
            raise GateProjectionError(
                f"gate request {request_id} already has decision "
                f"{self._decision_id_by_request[request_id]}"
            )
        return evaluate_gate_request(self._graph, self._state_projection, request)
