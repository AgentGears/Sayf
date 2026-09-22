from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sayf.artifacts import ArtifactVerification, ContentAddressedArtifactStore
from sayf.domain import Actor, EventDraft, LedgerEvent
from sayf.feedback import (
    ExplanationPath,
    FeedbackApplicationState,
    FeedbackApplicationStatus,
    FeedbackCaseSpec,
    M05Projection,
    M05ProjectionError,
    ReleaseSpec,
    ReleaseStatus,
    RuntimeObservationSpec,
    TimelineEntry,
    build_feedback_case_payload,
    build_release_payload,
    build_runtime_observation_payload,
    feedback_invalidation_relation_draft,
    managed_feedback_case_id_for_relation,
)
from sayf.gates import (
    GateDecisionStatus,
    GateProjection,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
    build_gate_request_payload,
    build_policy_snapshot_payload,
    build_verification_receipt_payload,
)
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.ids import new_id
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RELATION_CREATED_EVENT_TYPE,
    ArtifactDescriptor,
    Record,
    RecordDraft,
    RecordType,
    Relation,
    RelationDraft,
    RiskAssessmentPayload,
    record_event_payload,
    record_from_event,
    relation_event_payload,
    relation_from_event,
    semantic_record_event_payload,
)
from sayf.risk import (
    M06Projection,
    RiskAssessmentSpec,
    build_risk_assessment_payload,
    validate_verification_risk_composition,
)
from sayf.semantic_append import append_if_ledger_head
from sayf.state import (
    DEPENDENCY_BOUND_EVENT_TYPE,
    RECORD_INVALIDATED_EVENT_TYPE,
    RECORD_SUPERSEDED_EVENT_TYPE,
    EffectiveStateProjection,
    semantic_relation_event_payload,
)
from sayf.storage import LedgerReadError, SQLiteEventStore


def _validate_requested_record_id(record_id: Any) -> str:
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("record id must be a non-empty string")
    try:
        record_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("record id must be valid UTF-8 text") from exc
    return record_id


class CausalRepository:
    """Authoritative semantic facade over the immutable Sayf event ledger."""

    def __init__(
        self,
        event_store: SQLiteEventStore,
        artifact_store: ContentAddressedArtifactStore,
    ) -> None:
        self.event_store = event_store
        self.artifact_store = artifact_store

    @classmethod
    def from_paths(
        cls,
        db_path: str | Path,
        artifact_root: str | Path,
    ) -> CausalRepository:
        return cls(
            event_store=SQLiteEventStore(db_path),
            artifact_store=ContentAddressedArtifactStore(artifact_root),
        )

    def _m06_snapshot(self) -> tuple[M06Projection, int, str | None]:
        events = self.event_store.events()
        projection = M06Projection.from_events(events)
        head_hash = events[-1].event_hash if events else None
        return projection, len(events), head_hash

    def _m05_snapshot(self) -> tuple[M05Projection, int, str | None]:
        projection, event_count, head_hash = self._m06_snapshot()
        return projection.feedback_projection, event_count, head_hash

    def _semantic_snapshot(
        self,
    ) -> tuple[
        CausalGraph,
        EffectiveStateProjection,
        GateProjection,
        int,
        str | None,
    ]:
        projection, event_count, head_hash = self._m05_snapshot()
        return (
            projection.graph,
            projection.state_projection,
            projection.gate_projection,
            event_count,
            head_hash,
        )

    def _graph_snapshot(self) -> tuple[CausalGraph, int, str | None]:
        graph, _, _, event_count, head_hash = self._semantic_snapshot()
        return graph, event_count, head_hash

    def graph(self) -> CausalGraph:
        graph, _, _, _, _ = self._semantic_snapshot()
        return graph

    def effective_state(self) -> EffectiveStateProjection:
        _, state, _, _, _ = self._semantic_snapshot()
        return state

    def gates(self) -> GateProjection:
        _, _, gates, _, _ = self._semantic_snapshot()
        return gates

    def feedback(self) -> M05Projection:
        projection, _, _ = self._m05_snapshot()
        return projection

    def risk(self) -> M06Projection:
        projection, _, _ = self._m06_snapshot()
        return projection

    def create_record(self, draft: RecordDraft, *, actor: Actor) -> Record:
        snapshot = RecordDraft.model_validate(draft.model_dump(mode="python"))
        self.event_store.initialize()
        graph, expected_event_count, expected_head_hash = self._graph_snapshot()
        try:
            graph.record(snapshot.record_id)
        except GraphProjectionError:
            pass
        else:
            raise LedgerReadError(f"record id already exists: {snapshot.record_id}")

        event = append_if_ledger_head(
            self.event_store,
            EventDraft(
                event_id=snapshot.record_id,
                stream_id=f"record:{snapshot.record_id}",
                event_type=RECORD_CREATED_EVENT_TYPE,
                actor=actor,
                payload=record_event_payload(snapshot),
            ),
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )
        return record_from_event(event)

    def _append_semantic_record(
        self,
        record_type: RecordType,
        payload: BaseModel,
        *,
        actor: Actor,
        record_id: str | None,
        graph: CausalGraph,
        expected_event_count: int,
        expected_head_hash: str | None,
    ) -> Record:
        semantic_record_id = (
            new_id("rec")
            if record_id is None
            else _validate_requested_record_id(record_id)
        )
        try:
            graph.record(semantic_record_id)
        except GraphProjectionError:
            pass
        else:
            raise LedgerReadError(f"record id already exists: {semantic_record_id}")

        event = append_if_ledger_head(
            self.event_store,
            EventDraft(
                event_id=semantic_record_id,
                stream_id=f"record:{semantic_record_id}",
                event_type=RECORD_CREATED_EVENT_TYPE,
                actor=actor,
                payload=semantic_record_event_payload(record_type, payload),
            ),
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )
        return record_from_event(event)

    def record_risk_assessment(
        self,
        spec: RiskAssessmentSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = RiskAssessmentSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        projection, expected_event_count, expected_head_hash = self._m06_snapshot()
        payload = build_risk_assessment_payload(projection.graph, snapshot)
        return self._append_semantic_record(
            RecordType.RISK_ASSESSMENT,
            payload,
            actor=actor,
            record_id=record_id,
            graph=projection.graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def risk_assessment(self, record_id: str) -> RiskAssessmentPayload:
        return self.risk().risk_assessment(record_id)

    def record_verification(
        self,
        spec: VerificationReceiptSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = VerificationReceiptSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        graph, _, _, expected_event_count, expected_head_hash = self._semantic_snapshot()
        payload = build_verification_receipt_payload(graph, snapshot)
        validate_verification_risk_composition(
            graph,
            payload,
            receipt_id=record_id or "<pending-verification-receipt>",
        )
        return self._append_semantic_record(
            RecordType.VERIFICATION_RECEIPT,
            payload,
            actor=actor,
            record_id=record_id,
            graph=graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def register_policy_snapshot(
        self,
        spec: PolicySnapshotSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = PolicySnapshotSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        graph, _, _, expected_event_count, expected_head_hash = self._semantic_snapshot()
        payload = build_policy_snapshot_payload(snapshot)
        return self._append_semantic_record(
            RecordType.POLICY_SNAPSHOT,
            payload,
            actor=actor,
            record_id=record_id,
            graph=graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def request_gate(
        self,
        spec: GateRequestSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = GateRequestSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        graph, _, _, expected_event_count, expected_head_hash = self._semantic_snapshot()
        payload = build_gate_request_payload(graph, snapshot)
        return self._append_semantic_record(
            RecordType.GATE_REQUEST,
            payload,
            actor=actor,
            record_id=record_id,
            graph=graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def evaluate_gate(
        self,
        request_id: str,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        self.event_store.initialize()
        graph, _, gates, expected_event_count, expected_head_hash = self._semantic_snapshot()
        payload = gates.evaluate_request(request_id)
        return self._append_semantic_record(
            RecordType.GATE_DECISION,
            payload,
            actor=actor,
            record_id=record_id,
            graph=graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def gate_status(self, decision_id: str) -> GateDecisionStatus:
        return self.gates().status(decision_id)

    def register_release(
        self,
        spec: ReleaseSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = ReleaseSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        projection, expected_event_count, expected_head_hash = self._m05_snapshot()
        projection.validate_release_ref_available(snapshot.release_ref)
        gate_status = projection.gate_projection.status(snapshot.gate_decision_id)
        payload = build_release_payload(
            projection.graph,
            projection.state_projection,
            gate_status,
            snapshot,
        )
        return self._append_semantic_record(
            RecordType.RELEASE,
            payload,
            actor=actor,
            record_id=record_id,
            graph=projection.graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def release_status(self, release_id: str) -> ReleaseStatus:
        return self.feedback().release_status(release_id)

    def record_runtime_observation(
        self,
        spec: RuntimeObservationSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = RuntimeObservationSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        projection, expected_event_count, expected_head_hash = self._m05_snapshot()
        payload = build_runtime_observation_payload(projection.graph, snapshot)
        return self._append_semantic_record(
            RecordType.RUNTIME_OBSERVATION,
            payload,
            actor=actor,
            record_id=record_id,
            graph=projection.graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def open_feedback_case(
        self,
        spec: FeedbackCaseSpec,
        *,
        actor: Actor,
        record_id: str | None = None,
    ) -> Record:
        snapshot = FeedbackCaseSpec.model_validate(spec.model_dump(mode="python"))
        self.event_store.initialize()
        projection, expected_event_count, expected_head_hash = self._m05_snapshot()
        payload = build_feedback_case_payload(projection.graph, snapshot)
        return self._append_semantic_record(
            RecordType.FEEDBACK_CASE,
            payload,
            actor=actor,
            record_id=record_id,
            graph=projection.graph,
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def feedback_application_status(self, case_id: str) -> FeedbackApplicationStatus:
        return self.feedback().feedback_application_status(case_id)

    def _qualify_feedback_invalidation(self, case_id: str, *, actor: Actor) -> LedgerEvent:
        """Create feedback authority from one exact M0.5 eligibility snapshot."""
        projection, expected_event_count, expected_head_hash = self._m05_snapshot()
        status = projection.feedback_application_status(case_id)
        if status.state is FeedbackApplicationState.APPLIED:
            raise LedgerReadError(f"feedback case {case_id} is already applied")
        if status.state is not FeedbackApplicationState.PENDING_QUALIFICATION:
            raise LedgerReadError(
                f"feedback case {case_id} has no pending relation to qualify"
            )

        projection.validate_feedback_application_eligibility(case_id)
        state = projection.state_projection
        relation = state.validate_invalidation(status.relation_id)
        managed_case_id = managed_feedback_case_id_for_relation(
            projection.graph,
            relation.id,
        )
        if managed_case_id != case_id:
            raise M05ProjectionError(
                f"feedback relation {relation.id} is not managed by case {case_id}"
            )

        return append_if_ledger_head(
            self.event_store,
            EventDraft(
                stream_id=f"state:{relation.id}",
                event_type=RECORD_INVALIDATED_EVENT_TYPE,
                actor=actor,
                payload=semantic_relation_event_payload(relation),
            ),
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def apply_feedback(self, case_id: str, *, actor: Actor) -> FeedbackApplicationStatus:
        """Apply one feedback invalidation with crash-safe and concurrency-safe retries."""
        self.event_store.initialize()
        last_error: Exception | None = None

        for _ in range(4):
            projection, _, _ = self._m05_snapshot()
            status = projection.feedback_application_status(case_id)
            if status.state is FeedbackApplicationState.APPLIED:
                return status

            projection.validate_feedback_application_eligibility(case_id)
            case_record = projection.graph.record(case_id)
            draft = feedback_invalidation_relation_draft(case_record)

            if status.state is FeedbackApplicationState.NOT_APPLIED:
                try:
                    self.create_relation(draft, actor=actor)
                except (LedgerReadError, GraphProjectionError) as exc:
                    last_error = exc
                    latest = self.feedback().feedback_application_status(case_id)
                    if latest.state is FeedbackApplicationState.APPLIED:
                        return latest
                    # NOT_APPLIED can mean an unrelated exact-head race; retry.
                    # PENDING means compatible competing progress; retry qualification.
                continue

            try:
                self._qualify_feedback_invalidation(case_id, actor=actor)
            except LedgerReadError as exc:
                last_error = exc
                latest = self.feedback().feedback_application_status(case_id)
                if latest.state is FeedbackApplicationState.APPLIED:
                    return latest
                # PENDING means either unrelated head movement or compatible progress.
                # Retry from a new semantic snapshot so eligibility is checked again.
                continue
            return self.feedback().feedback_application_status(case_id)

        if last_error is not None:
            raise LedgerReadError(
                "feedback application did not converge after concurrent ledger changes"
            ) from last_error
        raise LedgerReadError("feedback application did not converge")

    def why(
        self,
        record_id: str,
        *,
        max_depth: int = 8,
        max_results: int = 100,
        max_expansions: int = 10_000,
    ) -> tuple[ExplanationPath, ...]:
        return self.feedback().why(
            record_id,
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
        return self.feedback().impact(
            record_id,
            max_depth=max_depth,
            max_results=max_results,
            max_expansions=max_expansions,
        )

    def timeline(self, record_id: str, *, max_entries: int = 200) -> tuple[TimelineEntry, ...]:
        return self.feedback().timeline(record_id, max_entries=max_entries)

    def create_relation(self, draft: RelationDraft, *, actor: Actor) -> Relation:
        snapshot = RelationDraft.model_validate(draft.model_dump(mode="python"))
        self.event_store.initialize()
        graph, expected_event_count, expected_head_hash = self._graph_snapshot()
        graph.record(snapshot.source_id)
        graph.record(snapshot.target_id)
        try:
            graph.relation(snapshot.relation_id)
        except GraphProjectionError:
            pass
        else:
            raise LedgerReadError(f"relation id already exists: {snapshot.relation_id}")

        event = append_if_ledger_head(
            self.event_store,
            EventDraft(
                event_id=snapshot.relation_id,
                stream_id=f"relation:{snapshot.relation_id}",
                event_type=RELATION_CREATED_EVENT_TYPE,
                actor=actor,
                payload=relation_event_payload(snapshot),
            ),
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )
        return relation_from_event(event)

    def _activate_state_relation(
        self,
        relation_id: str,
        *,
        event_type: str,
        actor: Actor,
    ) -> LedgerEvent:
        self.event_store.initialize()
        projection, expected_event_count, expected_head_hash = self._m05_snapshot()
        state = projection.state_projection

        if event_type == DEPENDENCY_BOUND_EVENT_TYPE:
            relation = state.validate_dependency_binding(relation_id)
        elif event_type == RECORD_INVALIDATED_EVENT_TYPE:
            managed_case_id = managed_feedback_case_id_for_relation(
                projection.graph,
                relation_id,
            )
            if managed_case_id is not None:
                raise M05ProjectionError(
                    f"managed feedback invalidation relation {relation_id} must be "
                    f"qualified through apply_feedback({managed_case_id!r})"
                )
            relation = state.validate_invalidation(relation_id)
        elif event_type == RECORD_SUPERSEDED_EVENT_TYPE:
            relation = state.validate_supersession(relation_id)
        else:
            raise ValueError(f"unsupported M0.3 state event type {event_type}")

        return append_if_ledger_head(
            self.event_store,
            EventDraft(
                stream_id=f"state:{relation.id}",
                event_type=event_type,
                actor=actor,
                payload=semantic_relation_event_payload(relation),
            ),
            expected_event_count=expected_event_count,
            expected_head_hash=expected_head_hash,
        )

    def bind_dependency(self, relation_id: str, *, actor: Actor) -> LedgerEvent:
        return self._activate_state_relation(
            relation_id,
            event_type=DEPENDENCY_BOUND_EVENT_TYPE,
            actor=actor,
        )

    def invalidate(self, relation_id: str, *, actor: Actor) -> LedgerEvent:
        return self._activate_state_relation(
            relation_id,
            event_type=RECORD_INVALIDATED_EVENT_TYPE,
            actor=actor,
        )

    def supersede(self, relation_id: str, *, actor: Actor) -> LedgerEvent:
        return self._activate_state_relation(
            relation_id,
            event_type=RECORD_SUPERSEDED_EVENT_TYPE,
            actor=actor,
        )

    def register_artifact_file(
        self,
        source: str | Path,
        *,
        actor: Actor,
        media_type: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        record_id: str | None = None,
    ) -> tuple[Record, ArtifactDescriptor]:
        validated_record_id = (
            None if record_id is None else _validate_requested_record_id(record_id)
        )
        descriptor = self.artifact_store.put_file(
            source,
            media_type=media_type,
            name=name,
            metadata={} if metadata is None else metadata,
        )
        draft_data: dict[str, Any] = {
            "record_type": RecordType.ARTIFACT,
            "payload": descriptor.model_dump(mode="python"),
        }
        if validated_record_id is not None:
            draft_data["record_id"] = validated_record_id
        record = self.create_record(RecordDraft.model_validate(draft_data), actor=actor)
        return record, descriptor

    def verify_artifact_record(self, record_id: str) -> ArtifactVerification:
        record = self.graph().record(record_id)
        if record.type is not RecordType.ARTIFACT:
            raise GraphProjectionError(f"record {record_id} is not an Artifact record")
        descriptor = ArtifactDescriptor.model_validate(record.payload)
        verification = self.artifact_store.verify(descriptor.digest)
        if verification.valid and verification.size_bytes != descriptor.size_bytes:
            return ArtifactVerification(
                digest=descriptor.digest,
                valid=False,
                size_bytes=verification.size_bytes,
                reason=(
                    "artifact byte length does not match the immutable Artifact record: "
                    f"expected {descriptor.size_bytes}, found {verification.size_bytes}"
                ),
            )
        return verification