from __future__ import annotations

from pathlib import Path
from typing import Any

from sayf.artifacts import ArtifactVerification, ContentAddressedArtifactStore
from sayf.domain import Actor, EventDraft, LedgerEvent
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RELATION_CREATED_EVENT_TYPE,
    ArtifactDescriptor,
    Record,
    RecordDraft,
    RecordType,
    Relation,
    RelationDraft,
    record_event_payload,
    record_from_event,
    relation_event_payload,
    relation_from_event,
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

    def _semantic_snapshot(
        self,
    ) -> tuple[CausalGraph, EffectiveStateProjection, int, str | None]:
        events = self.event_store.events()
        graph = CausalGraph.from_events(events)
        state = EffectiveStateProjection.from_events(events, graph=graph)
        head_hash = events[-1].event_hash if events else None
        return graph, state, len(events), head_hash

    def _graph_snapshot(self) -> tuple[CausalGraph, int, str | None]:
        graph, _, event_count, head_hash = self._semantic_snapshot()
        return graph, event_count, head_hash

    def graph(self) -> CausalGraph:
        graph, _, _, _ = self._semantic_snapshot()
        return graph

    def effective_state(self) -> EffectiveStateProjection:
        _, state, _, _ = self._semantic_snapshot()
        return state

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
        _, state, expected_event_count, expected_head_hash = self._semantic_snapshot()

        if event_type == DEPENDENCY_BOUND_EVENT_TYPE:
            relation = state.validate_dependency_binding(relation_id)
        elif event_type == RECORD_INVALIDATED_EVENT_TYPE:
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
