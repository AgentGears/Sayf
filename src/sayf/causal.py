from __future__ import annotations

from pathlib import Path

from sayf.artifacts import ArtifactVerification, ContentAddressedArtifactStore
from sayf.domain import Actor, EventDraft
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
from sayf.storage import LedgerReadError, SQLiteEventStore


class CausalRepository:
    """Authoritative typed-write facade over the immutable M0.1 event ledger."""

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

    def graph(self) -> CausalGraph:
        return CausalGraph.from_events(self.event_store.events())

    def create_record(self, draft: RecordDraft, *, actor: Actor) -> Record:
        snapshot = RecordDraft.model_validate(draft.model_dump(mode="python"))
        self.event_store.initialize()
        # Validate existing semantic history before extending it. Record identity is the
        # creation-event ID, so the ledger's event_id UNIQUE constraint is also the
        # cross-process uniqueness guard for record creation.
        graph = self.graph()
        try:
            graph.record(snapshot.record_id)
        except GraphProjectionError:
            pass
        else:
            raise LedgerReadError(f"record id already exists: {snapshot.record_id}")

        event = self.event_store.append(
            EventDraft(
                event_id=snapshot.record_id,
                stream_id=f"record:{snapshot.record_id}",
                event_type=RECORD_CREATED_EVENT_TYPE,
                actor=actor,
                payload=record_event_payload(snapshot),
            )
        )
        return record_from_event(event)

    def create_relation(self, draft: RelationDraft, *, actor: Actor) -> Relation:
        snapshot = RelationDraft.model_validate(draft.model_dump(mode="python"))
        self.event_store.initialize()
        graph = self.graph()
        graph.record(snapshot.source_id)
        graph.record(snapshot.target_id)
        try:
            graph.relation(snapshot.relation_id)
        except GraphProjectionError:
            pass
        else:
            raise LedgerReadError(f"relation id already exists: {snapshot.relation_id}")

        event = self.event_store.append(
            EventDraft(
                event_id=snapshot.relation_id,
                stream_id=f"relation:{snapshot.relation_id}",
                event_type=RELATION_CREATED_EVENT_TYPE,
                actor=actor,
                payload=relation_event_payload(snapshot),
            )
        )
        return relation_from_event(event)

    def register_artifact_file(
        self,
        source: str | Path,
        *,
        actor: Actor,
        media_type: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
        record_id: str | None = None,
    ) -> tuple[Record, ArtifactDescriptor]:
        if record_id is not None:
            RecordDraft(record_type=RecordType.CLAIM, payload={}, record_id=record_id)
        descriptor = self.artifact_store.put_file(
            source,
            media_type=media_type,
            name=name,
            metadata={} if metadata is None else metadata,
        )
        draft_data = {
            "record_type": RecordType.ARTIFACT,
            "payload": descriptor.model_dump(mode="python"),
        }
        if record_id is not None:
            draft_data["record_id"] = record_id
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
