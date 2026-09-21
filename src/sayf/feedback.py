from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sayf.domain import LedgerEvent, _normalize_json_object, _normalize_text
from sayf.gates import (
    GateDecisionFreshness,
    GateDecisionStatus,
    GateProjection,
    GateProjectionError,
    effective_state_hash,
    record_binding,
)
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.hashing import HASH_PREFIX, canonical_json
from sayf.records import (
    FeedbackCasePayload,
    FeedbackClassification,
    FeedbackEffect,
    GateDecisionPayload,
    GateOutcome,
    GateRequestPayload,
    Record,
    RecordBinding,
    RecordType,
    RelationDraft,
    RelationType,
    ReleasePayload,
    RuntimeObservationPayload,
    RuntimeOutcome,
    VerificationReceiptPayload,
)
from sayf.state import (
    DEPENDENCY_BOUND_EVENT_TYPE,
    RECORD_INVALIDATED_EVENT_TYPE,
    RECORD_SUPERSEDED_EVENT_TYPE,
    EffectiveStateProjection,
    FreshnessState,
    RevisionState,
    SemanticRelationPayload,
    ValidityState,
)

RELEASE_AUTHORITY_FINGERPRINT_VERSION = 1


class M05ProjectionError(GateProjectionError):
    """Raised when M0.5 release/feedback history cannot be projected safely."""


class ReleaseAuthorityFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class FeedbackApplicationState(StrEnum):
    NOT_APPLIED = "not_applied"
    PENDING_QUALIFICATION = "pending_qualification"
    APPLIED = "applied"


class ExplainEdgeKind(StrEnum):
    QUALIFIED_DEPENDENCY = "qualified_dependency"
    INVALIDATION_CAUSE = "invalidation_cause"
    SUPERSESSION_CAUSE = "supersession_cause"
    VERIFICATION_SUBJECT = "verification_subject"
    VERIFICATION_EVIDENCE = "verification_evidence"
    GATE_REQUEST_INPUT = "gate_request_input"
    GATE_EVALUATED_INPUT = "gate_evaluated_input"
    RELEASE_SUBJECT = "release_subject"
    RELEASE_GATE = "release_gate"
    RUNTIME_RELEASE = "runtime_release"
    RUNTIME_EVIDENCE = "runtime_evidence"
    FEEDBACK_OBSERVATION = "feedback_observation"
    FEEDBACK_TARGET = "feedback_target"


class ExplainPropagation(StrEnum):
    STATE = "state"
    AUTHORITY = "authority"
    HISTORICAL_REFERENCE = "historical_reference"


class ReleaseSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_ref: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    gate_decision_id: str = Field(min_length=1)
    environment: dict[str, Any] = Field(min_length=1)

    @field_validator("release_ref", "subject_id", "gate_decision_id")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _normalize_text(value, "release specification text")

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "release environment")


class RuntimeObservationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: str = Field(min_length=1)
    outcome: RuntimeOutcome
    summary: str = Field(min_length=1)
    environment: dict[str, Any] = Field(min_length=1)
    evidence_record_ids: tuple[str, ...] = ()

    @field_validator("release_id", "summary")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _normalize_text(value, "runtime observation text")

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "runtime observation environment")

    @field_validator("evidence_record_ids")
    @classmethod
    def normalize_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _normalize_text(item, "runtime evidence record id") for item in value
        )
        if any(not item for item in normalized):
            raise ValueError("runtime evidence record ids must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("runtime evidence record ids must be unique")
        return normalized


class FeedbackCaseSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    classification: FeedbackClassification
    proposed_effect: FeedbackEffect = FeedbackEffect.NONE
    rationale: str = Field(min_length=1)

    @field_validator("observation_id", "target_id", "rationale")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _normalize_text(value, "feedback case text")


class ReleaseStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: str
    release_ref: str
    subject_id: str
    gate_decision_id: str
    authority_freshness: ReleaseAuthorityFreshness
    stale_authority_record_ids: tuple[str, ...] = ()


class FeedbackApplicationStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    target_id: str
    relation_id: str
    state: FeedbackApplicationState
    invalidation_event_id: str | None = None


class ExplainEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    target_id: str
    kind: ExplainEdgeKind
    propagation: ExplainPropagation
    sequence: int = Field(ge=1)
    reference_id: str


class ExplanationPath(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_id: str
    end_id: str
    steps: tuple[ExplainEdge, ...]

    @property
    def depth(self) -> int:
        return len(self.steps)


class TimelineEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=1)
    event_id: str
    event_type: str
    roles: tuple[str, ...]
    related_record_ids: tuple[str, ...] = ()


def _sha256(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def release_authority_fingerprint(status: GateDecisionStatus) -> str:
    """Hash only the stable v1 gate-authority facts required by a Release."""
    return _sha256(
        {
            "schema_version": RELEASE_AUTHORITY_FINGERPRINT_VERSION,
            "decision_id": status.decision_id,
            "outcome": status.outcome.value,
            "freshness": status.freshness.value,
        }
    )


def _is_usable_state(state: Any) -> bool:
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
        raise M05ProjectionError(
            f"{role} references missing record {binding.record_id}"
        ) from exc
    if record.content_hash != binding.content_hash:
        raise M05ProjectionError(
            f"{role} binding for {record.id} does not match immutable content hash"
        )
    if expected_type is not None and record.type is not expected_type:
        raise M05ProjectionError(
            f"{role} record {record.id} must have type {expected_type.value}, "
            f"found {record.type.value}"
        )
    if before_sequence is not None and record.created_sequence >= before_sequence:
        raise M05ProjectionError(
            f"{role} record {record.id} must exist before the record that binds it"
        )
    return record


def _release_payload(record: Record) -> ReleasePayload:
    try:
        return ReleasePayload.model_validate(record.payload)
    except ValidationError as exc:
        raise M05ProjectionError(f"release {record.id} has invalid payload: {exc}") from exc


def _runtime_payload(record: Record) -> RuntimeObservationPayload:
    try:
        return RuntimeObservationPayload.model_validate(record.payload)
    except ValidationError as exc:
        raise M05ProjectionError(
            f"runtime observation {record.id} has invalid payload: {exc}"
        ) from exc


def _feedback_payload(record: Record) -> FeedbackCasePayload:
    try:
        return FeedbackCasePayload.model_validate(record.payload)
    except ValidationError as exc:
        raise M05ProjectionError(
            f"feedback case {record.id} has invalid payload: {exc}"
        ) from exc


def _gate_status_for_state(
    graph: CausalGraph,
    state: EffectiveStateProjection,
    decision_id: str,
) -> GateDecisionStatus:
    """Derive one decision status from an already-validated gate record and state."""
    decision = graph.record(decision_id)
    if decision.type is not RecordType.GATE_DECISION:
        raise M05ProjectionError(f"record {decision_id} is not a GateDecision")
    payload = GateDecisionPayload.model_validate(decision.payload)
    stale_input_ids: list[str] = []

    decision_state = state.state(decision.id)
    if not _is_usable_state(decision_state):
        stale_input_ids.append(decision.id)

    for expected_input in payload.evaluated_inputs:
        current_record = graph.record(expected_input.record_id)
        current_state = state.state(expected_input.record_id)
        if (
            current_record.content_hash != expected_input.content_hash
            or effective_state_hash(current_state) != expected_input.effective_state_hash
        ):
            stale_input_ids.append(expected_input.record_id)

    return GateDecisionStatus(
        decision_id=decision.id,
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


def build_release_payload(
    graph: CausalGraph,
    state: EffectiveStateProjection,
    gate_status: GateDecisionStatus,
    spec: ReleaseSpec,
) -> ReleasePayload:
    subject = graph.record(spec.subject_id)
    if subject.type is not RecordType.CHANGE_SET:
        raise M05ProjectionError(
            f"release subject {subject.id} must have type ChangeSet, found {subject.type.value}"
        )
    decision = graph.record(spec.gate_decision_id)
    if decision.type is not RecordType.GATE_DECISION:
        raise M05ProjectionError(
            f"release gate {decision.id} must have type GateDecision, found {decision.type.value}"
        )
    if gate_status.decision_id != decision.id:
        raise M05ProjectionError(
            f"gate status {gate_status.decision_id} does not describe release gate {decision.id}"
        )
    decision_payload = GateDecisionPayload.model_validate(decision.payload)
    if decision_payload.subject != record_binding(subject):
        raise M05ProjectionError(
            f"gate decision {decision.id} does not authorize release subject {subject.id}"
        )
    if gate_status.outcome is not GateOutcome.PERMIT:
        raise M05ProjectionError(
            f"gate decision {decision.id} is {gate_status.outcome.value}, not permit"
        )
    if gate_status.freshness is not GateDecisionFreshness.FRESH:
        raise M05ProjectionError(f"gate decision {decision.id} is stale")
    subject_state = state.state(subject.id)
    if not _is_usable_state(subject_state):
        raise M05ProjectionError(f"release subject {subject.id} is not currently usable")
    return ReleasePayload(
        release_ref=spec.release_ref,
        subject=record_binding(subject),
        gate_decision=record_binding(decision),
        authority_fingerprint_version=RELEASE_AUTHORITY_FINGERPRINT_VERSION,
        authority_fingerprint=release_authority_fingerprint(gate_status),
        environment=spec.environment,
    )


def build_runtime_observation_payload(
    graph: CausalGraph,
    spec: RuntimeObservationSpec,
) -> RuntimeObservationPayload:
    release = graph.record(spec.release_id)
    if release.type is not RecordType.RELEASE:
        raise M05ProjectionError(
            f"runtime observation release {release.id} must have type Release, "
            f"found {release.type.value}"
        )
    evidence = tuple(graph.record(record_id) for record_id in spec.evidence_record_ids)
    return RuntimeObservationPayload(
        release=record_binding(release),
        outcome=spec.outcome,
        summary=spec.summary,
        environment=spec.environment,
        evidence=tuple(record_binding(record) for record in evidence),
    )


def build_feedback_case_payload(
    graph: CausalGraph,
    spec: FeedbackCaseSpec,
) -> FeedbackCasePayload:
    observation = graph.record(spec.observation_id)
    if observation.type is not RecordType.RUNTIME_OBSERVATION:
        raise M05ProjectionError(
            f"feedback observation {observation.id} must have type RuntimeObservation, "
            f"found {observation.type.value}"
        )
    target = graph.record(spec.target_id)
    return FeedbackCasePayload(
        observation=record_binding(observation),
        target=record_binding(target),
        classification=spec.classification,
        proposed_effect=spec.proposed_effect,
        rationale=spec.rationale,
    )


def feedback_invalidation_relation_id(case_id: str) -> str:
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return f"rel_feedback_{digest}"


def feedback_invalidation_relation_draft(case_record: Record) -> RelationDraft:
    if case_record.type is not RecordType.FEEDBACK_CASE:
        raise M05ProjectionError(f"record {case_record.id} is not a FeedbackCase")
    payload = _feedback_payload(case_record)
    if payload.proposed_effect is not FeedbackEffect.INVALIDATE_TARGET:
        raise M05ProjectionError(
            f"feedback case {case_record.id} does not propose target invalidation"
        )
    if payload.classification not in {
        FeedbackClassification.FALSIFIES,
        FeedbackClassification.CONTRADICTS,
        FeedbackClassification.VIOLATES,
    }:
        raise M05ProjectionError(
            f"feedback case {case_record.id} classification does not support invalidation"
        )
    return RelationDraft(
        relation_id=feedback_invalidation_relation_id(case_record.id),
        relation_type=RelationType.INVALIDATES,
        source_id=case_record.id,
        target_id=payload.target.record_id,
        metadata={
            "schema_version": 1,
            "feedback_case_id": case_record.id,
            "feedback_case_content_hash": case_record.content_hash,
        },
    )


def managed_feedback_case_id_for_relation(
    graph: CausalGraph,
    relation_id: str,
) -> str | None:
    """Return the FeedbackCase owning a managed deterministic invalidation relation."""
    relation = graph.relation(relation_id)
    source = graph.record(relation.source_id)
    if source.type is not RecordType.FEEDBACK_CASE:
        return None
    if relation.id != feedback_invalidation_relation_id(source.id):
        return None

    expected = feedback_invalidation_relation_draft(source)
    if (
        relation.type is not expected.relation_type
        or relation.source_id != expected.source_id
        or relation.target_id != expected.target_id
        or relation.metadata != expected.metadata
    ):
        raise M05ProjectionError(
            f"managed feedback relation {relation.id} has incompatible immutable content"
        )
    return source.id


def validate_feedback_application_eligibility_for_state(
    graph: CausalGraph,
    state: EffectiveStateProjection,
    case_id: str,
) -> None:
    """Require source records for new feedback authority to be usable in one state."""
    case = graph.record(case_id)
    if case.type is not RecordType.FEEDBACK_CASE:
        raise M05ProjectionError(f"record {case_id} is not a FeedbackCase")
    payload = _feedback_payload(case)
    observation = _resolve_binding(
        graph,
        payload.observation,
        expected_type=RecordType.RUNTIME_OBSERVATION,
        before_sequence=case.created_sequence,
        role=f"feedback case {case.id} observation",
    )
    runtime = _runtime_payload(observation)

    source_ids = [case.id, observation.id]
    source_ids.extend(binding.record_id for binding in runtime.evidence)
    unusable = [
        record_id
        for record_id in source_ids
        if not _is_usable_state(state.state(record_id))
    ]
    if unusable:
        raise M05ProjectionError(
            f"feedback case {case.id} cannot create invalidation authority because "
            "source records are not usable: " + ", ".join(unusable)
        )


def _validate_feedback_qualification_history(
    events: tuple[LedgerEvent, ...],
    graph: CausalGraph,
) -> None:
    """Fail closed if historical managed feedback authority bypassed eligibility."""
    for case in (record for record in graph.records if record.type is RecordType.FEEDBACK_CASE):
        relation_id = feedback_invalidation_relation_id(case.id)
        try:
            graph.relation(relation_id)
        except GraphProjectionError:
            continue
        managed_case_id = managed_feedback_case_id_for_relation(graph, relation_id)
        if managed_case_id != case.id:
            raise M05ProjectionError(
                f"feedback relation id {relation_id} is occupied by incompatible content"
            )

    for event in events:
        if event.event_type != RECORD_INVALIDATED_EVENT_TYPE:
            continue
        payload = SemanticRelationPayload.model_validate(event.payload)
        try:
            case_id = managed_feedback_case_id_for_relation(graph, payload.relation_id)
        except GraphProjectionError:
            continue
        if case_id is None:
            continue

        prefix = events[: event.sequence - 1]
        prefix_state = EffectiveStateProjection.from_events(prefix)
        prefix_case_id = managed_feedback_case_id_for_relation(
            prefix_state.graph,
            payload.relation_id,
        )
        if prefix_case_id != case_id:
            raise M05ProjectionError(
                f"managed feedback qualification {event.event_id} does not bind its "
                "expected FeedbackCase at the historical activation prefix"
            )
        validate_feedback_application_eligibility_for_state(
            prefix_state.graph,
            prefix_state,
            case_id,
        )


class M05Projection:
    """Derived M0.5 release, feedback, and explanation state over immutable history."""

    def __init__(
        self,
        *,
        events: tuple[LedgerEvent, ...],
        gates: GateProjection,
        release_statuses: dict[str, ReleaseStatus],
        explain_edges: tuple[ExplainEdge, ...],
    ) -> None:
        self._events = events
        self._gates = gates
        self._state = gates.state_projection
        self._graph = gates.graph
        self._release_statuses = dict(release_statuses)
        self._explain_edges = explain_edges
        outgoing: dict[str, list[ExplainEdge]] = defaultdict(list)
        incoming: dict[str, list[ExplainEdge]] = defaultdict(list)
        for edge in explain_edges:
            outgoing[edge.source_id].append(edge)
            incoming[edge.target_id].append(edge)

        def edge_order(edge: ExplainEdge) -> tuple[int, str, str, str, str]:
            return (
                edge.sequence,
                edge.kind.value,
                edge.reference_id,
                edge.source_id,
                edge.target_id,
            )

        self._outgoing = {
            record_id: tuple(sorted(edges, key=edge_order))
            for record_id, edges in outgoing.items()
        }
        self._incoming = {
            record_id: tuple(sorted(edges, key=edge_order))
            for record_id, edges in incoming.items()
        }

    @classmethod
    def from_events(cls, events: Iterable[LedgerEvent]) -> M05Projection:
        event_list = tuple(events)
        gates = GateProjection.from_events(event_list)
        graph = gates.graph
        seen_release_refs: dict[str, str] = {}

        for record in graph.records:
            try:
                if record.type is RecordType.RELEASE:
                    actual = _release_payload(record)
                    if actual.release_ref in seen_release_refs:
                        raise M05ProjectionError(
                            f"release_ref {actual.release_ref!r} is already used by "
                            f"release {seen_release_refs[actual.release_ref]}"
                        )
                    prefix = event_list[: record.created_sequence - 1]
                    prefix_state = EffectiveStateProjection.from_events(prefix)
                    _resolve_binding(
                        prefix_state.graph,
                        actual.subject,
                        expected_type=RecordType.CHANGE_SET,
                        before_sequence=record.created_sequence,
                        role=f"release {record.id} subject",
                    )
                    decision = _resolve_binding(
                        prefix_state.graph,
                        actual.gate_decision,
                        expected_type=RecordType.GATE_DECISION,
                        before_sequence=record.created_sequence,
                        role=f"release {record.id} gate decision",
                    )
                    prefix_gate_status = _gate_status_for_state(
                        prefix_state.graph,
                        prefix_state,
                        decision.id,
                    )
                    expected = build_release_payload(
                        prefix_state.graph,
                        prefix_state,
                        prefix_gate_status,
                        ReleaseSpec(
                            release_ref=actual.release_ref,
                            subject_id=actual.subject.record_id,
                            gate_decision_id=actual.gate_decision.record_id,
                            environment=actual.environment,
                        ),
                    )
                    if actual != expected:
                        raise M05ProjectionError(
                            f"release {record.id} does not match deterministic authority "
                            "evaluation at release time"
                        )
                    seen_release_refs[actual.release_ref] = record.id
                elif record.type is RecordType.RUNTIME_OBSERVATION:
                    payload = _runtime_payload(record)
                    _resolve_binding(
                        graph,
                        payload.release,
                        expected_type=RecordType.RELEASE,
                        before_sequence=record.created_sequence,
                        role=f"runtime observation {record.id} release",
                    )
                    for binding in payload.evidence:
                        _resolve_binding(
                            graph,
                            binding,
                            before_sequence=record.created_sequence,
                            role=f"runtime observation {record.id} evidence",
                        )
                elif record.type is RecordType.FEEDBACK_CASE:
                    payload = _feedback_payload(record)
                    _resolve_binding(
                        graph,
                        payload.observation,
                        expected_type=RecordType.RUNTIME_OBSERVATION,
                        before_sequence=record.created_sequence,
                        role=f"feedback case {record.id} observation",
                    )
                    _resolve_binding(
                        graph,
                        payload.target,
                        before_sequence=record.created_sequence,
                        role=f"feedback case {record.id} target",
                    )
            except M05ProjectionError:
                raise
            except (ValidationError, GraphProjectionError, ValueError) as exc:
                raise M05ProjectionError(
                    f"invalid M0.5 semantic record at ledger sequence "
                    f"{record.created_sequence} ({record.id}): {exc}"
                ) from exc

        _validate_feedback_qualification_history(event_list, graph)

        release_statuses: dict[str, ReleaseStatus] = {}
        for record in graph.records:
            if record.type is not RecordType.RELEASE:
                continue
            payload = _release_payload(record)
            stale_ids: list[str] = []
            release_state = gates.state_projection.state(record.id)
            if not _is_usable_state(release_state):
                stale_ids.append(record.id)
            gate_status = gates.status(payload.gate_decision.record_id)
            if release_authority_fingerprint(gate_status) != payload.authority_fingerprint:
                stale_ids.extend(gate_status.stale_input_record_ids)
                stale_ids.append(payload.gate_decision.record_id)
            release_statuses[record.id] = ReleaseStatus(
                release_id=record.id,
                release_ref=payload.release_ref,
                subject_id=payload.subject.record_id,
                gate_decision_id=payload.gate_decision.record_id,
                authority_freshness=(
                    ReleaseAuthorityFreshness.STALE
                    if stale_ids
                    else ReleaseAuthorityFreshness.FRESH
                ),
                stale_authority_record_ids=tuple(dict.fromkeys(stale_ids)),
            )

        explain_edges = cls._derive_explain_edges(event_list, gates)
        return cls(
            events=event_list,
            gates=gates,
            release_statuses=release_statuses,
            explain_edges=explain_edges,
        )

    @staticmethod
    def _derive_explain_edges(
        events: tuple[LedgerEvent, ...],
        gates: GateProjection,
    ) -> tuple[ExplainEdge, ...]:
        graph = gates.graph
        edges: list[ExplainEdge] = []

        for event in events:
            if event.event_type not in {
                DEPENDENCY_BOUND_EVENT_TYPE,
                RECORD_INVALIDATED_EVENT_TYPE,
                RECORD_SUPERSEDED_EVENT_TYPE,
            }:
                continue
            payload = SemanticRelationPayload.model_validate(event.payload)
            relation = graph.relation(payload.relation_id)
            if event.event_type == DEPENDENCY_BOUND_EVENT_TYPE:
                edges.append(
                    ExplainEdge(
                        source_id=relation.source_id,
                        target_id=relation.target_id,
                        kind=ExplainEdgeKind.QUALIFIED_DEPENDENCY,
                        propagation=ExplainPropagation.STATE,
                        sequence=event.sequence,
                        reference_id=event.event_id,
                    )
                )
            elif event.event_type == RECORD_INVALIDATED_EVENT_TYPE:
                edges.append(
                    ExplainEdge(
                        source_id=relation.target_id,
                        target_id=relation.source_id,
                        kind=ExplainEdgeKind.INVALIDATION_CAUSE,
                        propagation=ExplainPropagation.STATE,
                        sequence=event.sequence,
                        reference_id=event.event_id,
                    )
                )
            else:
                edges.append(
                    ExplainEdge(
                        source_id=relation.target_id,
                        target_id=relation.source_id,
                        kind=ExplainEdgeKind.SUPERSESSION_CAUSE,
                        propagation=ExplainPropagation.STATE,
                        sequence=event.sequence,
                        reference_id=event.event_id,
                    )
                )

        def add_binding(
            source: Record,
            target_id: str,
            kind: ExplainEdgeKind,
            propagation: ExplainPropagation,
        ) -> None:
            edges.append(
                ExplainEdge(
                    source_id=source.id,
                    target_id=target_id,
                    kind=kind,
                    propagation=propagation,
                    sequence=source.created_sequence,
                    reference_id=source.id,
                )
            )

        for record in graph.records:
            if record.type is RecordType.VERIFICATION_RECEIPT:
                payload = VerificationReceiptPayload.model_validate(record.payload)
                add_binding(
                    record,
                    payload.subject.record_id,
                    ExplainEdgeKind.VERIFICATION_SUBJECT,
                    ExplainPropagation.AUTHORITY,
                )
                for binding in payload.evidence:
                    add_binding(
                        record,
                        binding.record_id,
                        ExplainEdgeKind.VERIFICATION_EVIDENCE,
                        ExplainPropagation.AUTHORITY,
                    )
            elif record.type is RecordType.GATE_REQUEST:
                payload = GateRequestPayload.model_validate(record.payload)
                for binding in (
                    payload.subject,
                    payload.policy_snapshot,
                    *payload.verification_receipts,
                ):
                    add_binding(
                        record,
                        binding.record_id,
                        ExplainEdgeKind.GATE_REQUEST_INPUT,
                        ExplainPropagation.AUTHORITY,
                    )
            elif record.type is RecordType.GATE_DECISION:
                payload = GateDecisionPayload.model_validate(record.payload)
                for binding in payload.evaluated_inputs:
                    add_binding(
                        record,
                        binding.record_id,
                        ExplainEdgeKind.GATE_EVALUATED_INPUT,
                        ExplainPropagation.AUTHORITY,
                    )
            elif record.type is RecordType.RELEASE:
                payload = _release_payload(record)
                add_binding(
                    record,
                    payload.subject.record_id,
                    ExplainEdgeKind.RELEASE_SUBJECT,
                    ExplainPropagation.AUTHORITY,
                )
                add_binding(
                    record,
                    payload.gate_decision.record_id,
                    ExplainEdgeKind.RELEASE_GATE,
                    ExplainPropagation.AUTHORITY,
                )
            elif record.type is RecordType.RUNTIME_OBSERVATION:
                payload = _runtime_payload(record)
                add_binding(
                    record,
                    payload.release.record_id,
                    ExplainEdgeKind.RUNTIME_RELEASE,
                    ExplainPropagation.HISTORICAL_REFERENCE,
                )
                for binding in payload.evidence:
                    add_binding(
                        record,
                        binding.record_id,
                        ExplainEdgeKind.RUNTIME_EVIDENCE,
                        ExplainPropagation.HISTORICAL_REFERENCE,
                    )
            elif record.type is RecordType.FEEDBACK_CASE:
                payload = _feedback_payload(record)
                add_binding(
                    record,
                    payload.observation.record_id,
                    ExplainEdgeKind.FEEDBACK_OBSERVATION,
                    ExplainPropagation.HISTORICAL_REFERENCE,
                )
                add_binding(
                    record,
                    payload.target.record_id,
                    ExplainEdgeKind.FEEDBACK_TARGET,
                    ExplainPropagation.HISTORICAL_REFERENCE,
                )

        unique: dict[tuple[object, ...], ExplainEdge] = {}
        for edge in edges:
            key = (
                edge.source_id,
                edge.target_id,
                edge.kind,
                edge.sequence,
                edge.reference_id,
            )
            unique[key] = edge
        return tuple(
            sorted(
                unique.values(),
                key=lambda edge: (
                    edge.sequence,
                    edge.kind.value,
                    edge.reference_id,
                    edge.source_id,
                    edge.target_id,
                ),
            )
        )

    @property
    def graph(self) -> CausalGraph:
        return self._graph

    @property
    def state_projection(self) -> EffectiveStateProjection:
        return self._state

    @property
    def gate_projection(self) -> GateProjection:
        return self._gates

    @property
    def explain_edges(self) -> tuple[ExplainEdge, ...]:
        return tuple(edge.model_copy(deep=True) for edge in self._explain_edges)

    def release_status(self, release_id: str) -> ReleaseStatus:
        record = self._graph.record(release_id)
        if record.type is not RecordType.RELEASE:
            raise M05ProjectionError(f"record {release_id} is not a Release")
        return self._release_statuses[release_id].model_copy(deep=True)

    def validate_release_ref_available(self, release_ref: str) -> None:
        normalized = _normalize_text(release_ref, "release ref")
        for status in self._release_statuses.values():
            if status.release_ref == normalized:
                raise M05ProjectionError(
                    f"release_ref {normalized!r} is already used by release {status.release_id}"
                )

    def feedback_application_status(self, case_id: str) -> FeedbackApplicationStatus:
        case = self._graph.record(case_id)
        if case.type is not RecordType.FEEDBACK_CASE:
            raise M05ProjectionError(f"record {case_id} is not a FeedbackCase")
        payload = _feedback_payload(case)
        relation_id = feedback_invalidation_relation_id(case.id)
        try:
            relation = self._graph.relation(relation_id)
        except GraphProjectionError:
            return FeedbackApplicationStatus(
                case_id=case.id,
                target_id=payload.target.record_id,
                relation_id=relation_id,
                state=FeedbackApplicationState.NOT_APPLIED,
            )

        managed_case_id = managed_feedback_case_id_for_relation(self._graph, relation_id)
        if managed_case_id != case.id:
            raise M05ProjectionError(
                f"feedback relation id {relation_id} is occupied by incompatible content"
            )

        target_state = self._state.state(payload.target.record_id)
        if relation_id not in target_state.invalidation_relation_ids:
            return FeedbackApplicationStatus(
                case_id=case.id,
                target_id=payload.target.record_id,
                relation_id=relation_id,
                state=FeedbackApplicationState.PENDING_QUALIFICATION,
            )
        index = target_state.invalidation_relation_ids.index(relation_id)
        return FeedbackApplicationStatus(
            case_id=case.id,
            target_id=payload.target.record_id,
            relation_id=relation_id,
            state=FeedbackApplicationState.APPLIED,
            invalidation_event_id=target_state.invalidation_event_ids[index],
        )

    def validate_feedback_application_eligibility(self, case_id: str) -> None:
        """Require all source evidence for new feedback authority to remain usable."""
        validate_feedback_application_eligibility_for_state(
            self._graph,
            self._state,
            case_id,
        )

    @staticmethod
    def _validate_query_bounds(
        max_depth: int,
        max_results: int,
        max_expansions: int,
    ) -> None:
        if not 0 <= max_depth <= 32:
            raise ValueError("max_depth must be between 0 and 32")
        if not 1 <= max_results <= 1000:
            raise ValueError("max_results must be between 1 and 1000")
        if not 1 <= max_expansions <= 100_000:
            raise ValueError("max_expansions must be between 1 and 100000")

    def _walk(
        self,
        record_id: str,
        *,
        reverse: bool,
        max_depth: int,
        max_results: int,
        max_expansions: int,
    ) -> tuple[ExplanationPath, ...]:
        self._validate_query_bounds(max_depth, max_results, max_expansions)
        self._graph.record(record_id)
        if max_depth == 0:
            return ()
        adjacency = self._incoming if reverse else self._outgoing
        queue: deque[
            tuple[str, tuple[ExplainEdge, ...], frozenset[str]]
        ] = deque([(record_id, (), frozenset({record_id}))])
        result: list[ExplanationPath] = []
        expansions = 0
        while queue:
            current_id, path, path_record_ids = queue.popleft()
            if len(path) >= max_depth:
                continue
            for edge in adjacency.get(current_id, ()):
                expansions += 1
                if expansions > max_expansions:
                    raise M05ProjectionError(
                        "explainability query exceeded the configured expansion bound"
                    )
                next_id = edge.source_id if reverse else edge.target_id
                if next_id in path_record_ids:
                    continue
                next_path = (*path, edge)
                result.append(
                    ExplanationPath(
                        start_id=record_id,
                        end_id=next_id,
                        steps=next_path,
                    )
                )
                if len(result) > max_results:
                    raise M05ProjectionError(
                        "explainability query exceeded the configured result bound"
                    )
                queue.append(
                    (next_id, next_path, path_record_ids | frozenset({next_id}))
                )
        return tuple(result)

    def why(
        self,
        record_id: str,
        *,
        max_depth: int = 8,
        max_results: int = 100,
        max_expansions: int = 10_000,
    ) -> tuple[ExplanationPath, ...]:
        """Return bounded deterministic recorded relationship paths upstream."""
        return self._walk(
            record_id,
            reverse=False,
            max_depth=max_depth,
            max_results=max_results,
            max_expansions=max_expansions,
        )

    def impact(
        self,
        record_id: str,
        *,
        max_depth: int = 8,
        max_results: int = 100,
        max_expansions: int = 10_000,
    ) -> tuple[ExplanationPath, ...]:
        """Return bounded deterministic recorded relationship paths downstream."""
        return self._walk(
            record_id,
            reverse=True,
            max_depth=max_depth,
            max_results=max_results,
            max_expansions=max_expansions,
        )

    def timeline(self, record_id: str, *, max_entries: int = 200) -> tuple[TimelineEntry, ...]:
        if not 1 <= max_entries <= 5000:
            raise ValueError("max_entries must be between 1 and 5000")
        record = self._graph.record(record_id)
        event_by_id = {event.event_id: event for event in self._events}
        roles_by_event: dict[str, set[str]] = defaultdict(set)
        related_by_event: dict[str, set[str]] = defaultdict(set)

        roles_by_event[record.creating_event_id].add("record_created")

        for relation in self._graph.relations:
            if relation.source_id == record_id:
                roles_by_event[relation.creating_event_id].add(
                    f"relation_source:{relation.type.value}"
                )
                related_by_event[relation.creating_event_id].add(relation.target_id)
            if relation.target_id == record_id:
                roles_by_event[relation.creating_event_id].add(
                    f"relation_target:{relation.type.value}"
                )
                related_by_event[relation.creating_event_id].add(relation.source_id)

        for event in self._events:
            if event.event_type not in {
                DEPENDENCY_BOUND_EVENT_TYPE,
                RECORD_INVALIDATED_EVENT_TYPE,
                RECORD_SUPERSEDED_EVENT_TYPE,
            }:
                continue
            payload = SemanticRelationPayload.model_validate(event.payload)
            relation = self._graph.relation(payload.relation_id)
            if relation.source_id == record_id:
                roles_by_event[event.event_id].add(f"state_source:{event.event_type}")
                related_by_event[event.event_id].add(relation.target_id)
            if relation.target_id == record_id:
                roles_by_event[event.event_id].add(f"state_target:{event.event_type}")
                related_by_event[event.event_id].add(relation.source_id)

        for edge in self._explain_edges:
            if edge.target_id == record_id and edge.reference_id in event_by_id:
                roles_by_event[edge.reference_id].add(f"bound_by:{edge.kind.value}")
                related_by_event[edge.reference_id].add(edge.source_id)
            if edge.source_id == record_id and edge.reference_id in event_by_id:
                roles_by_event[edge.reference_id].add(f"binds:{edge.kind.value}")
                related_by_event[edge.reference_id].add(edge.target_id)

        entries: list[TimelineEntry] = []
        for event_id, roles in roles_by_event.items():
            event = event_by_id[event_id]
            entries.append(
                TimelineEntry(
                    sequence=event.sequence,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    roles=tuple(sorted(roles)),
                    related_record_ids=tuple(sorted(related_by_event.get(event_id, set()))),
                )
            )
        entries.sort(key=lambda item: (item.sequence, item.event_id))
        if len(entries) > max_entries:
            raise M05ProjectionError("timeline query exceeded the configured result bound")
        return tuple(entries)
