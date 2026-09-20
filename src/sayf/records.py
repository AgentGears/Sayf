from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sayf.domain import Actor, LedgerEvent, _normalize_json_object, _normalize_text
from sayf.hashing import HASH_PREFIX, canonical_json
from sayf.ids import new_id

RECORD_CREATED_EVENT_TYPE = "sayf.record.created.v1"
RELATION_CREATED_EVENT_TYPE = "sayf.relation.created.v1"
RECORD_SCHEMA_VERSION = 1
RELATION_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RecordType(StrEnum):
    IDEA_SEED = "IdeaSeed"
    PROBLEM_FRAME = "ProblemFrame"
    CLAIM = "Claim"
    ASSUMPTION = "Assumption"
    HYPOTHESIS = "Hypothesis"
    EVIDENCE = "Evidence"
    EXPERIMENT = "Experiment"
    OBSERVATION = "Observation"
    REQUIREMENT = "Requirement"
    CONSTRAINT = "Constraint"
    DECISION = "Decision"
    INTENT_REVISION = "IntentRevision"
    BASELINE_SNAPSHOT = "BaselineSnapshot"
    TASK_GRAPH = "TaskGraph"
    TASK = "Task"
    EXECUTION_ATTEMPT = "ExecutionAttempt"
    CHANGE_SET = "ChangeSet"
    VERIFICATION_RECEIPT = "VerificationReceipt"
    RISK_ASSESSMENT = "RiskAssessment"
    POLICY_SNAPSHOT = "PolicySnapshot"
    GATE_REQUEST = "GateRequest"
    GATE_DECISION = "GateDecision"
    RELEASE = "Release"
    RUNTIME_OBSERVATION = "RuntimeObservation"
    FEEDBACK_CASE = "FeedbackCase"
    INTERACTION_RECEIPT = "InteractionReceipt"
    ARTIFACT = "Artifact"
    EXTERNAL_REFERENCE = "ExternalReference"


class RelationType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    FALSIFIES = "falsifies"
    ASSUMES = "assumes"
    DERIVED_FROM = "derived_from"
    OBSERVES = "observes"
    SUPERSEDES = "supersedes"
    ACCEPTS = "accepts"
    REJECTS = "rejects"
    GOVERNED_BY = "governed_by"
    IMPLEMENTS = "implements"
    DEPENDS_ON = "depends_on"
    PRODUCES = "produces"
    MODIFIES = "modifies"
    VERIFIED_BY = "verified_by"
    COVERS = "covers"
    PERMITS = "permits"
    BLOCKS = "blocks"
    RELEASES = "releases"
    VIOLATES = "violates"
    REOPENS = "reopens"
    INVALIDATES = "invalidates"
    CREATED_FROM = "created_from"
    DISCUSSED_IN = "discussed_in"
    REFERENCES = "references"


class ArtifactDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    digest: str
    size_bytes: int = Field(ge=0)
    media_type: str | None = None
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        value = _normalize_text(value, "artifact digest")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("artifact digest must be lowercase sha256:<64 hex>")
        return value

    @field_validator("media_type", "name")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _normalize_text(value, "artifact text")
        if not value:
            raise ValueError("artifact text fields must not be empty")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "artifact metadata")


class RecordDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    record_type: RecordType
    payload: dict[str, Any] = Field(default_factory=dict)
    record_id: str = Field(default_factory=lambda: new_id("rec"), min_length=1)
    schema_version: Literal[RECORD_SCHEMA_VERSION] = RECORD_SCHEMA_VERSION

    @field_validator("record_id")
    @classmethod
    def normalize_record_id(cls, value: str) -> str:
        return _normalize_text(value, "record id")

    @field_validator("payload", mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "record payload")

    @model_validator(mode="after")
    def validate_typed_payload(self) -> RecordDraft:
        if self.record_type is RecordType.ARTIFACT:
            ArtifactDescriptor.model_validate(self.payload)
        return self


class RelationDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    relation_type: RelationType
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    relation_id: str = Field(default_factory=lambda: new_id("rel"), min_length=1)
    schema_version: Literal[RELATION_SCHEMA_VERSION] = RELATION_SCHEMA_VERSION

    @field_validator("source_id", "target_id", "relation_id")
    @classmethod
    def normalize_ids(cls, value: str) -> str:
        return _normalize_text(value, "relation id")

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "relation metadata")


class RecordCreatedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: RecordType
    schema_version: Literal[RECORD_SCHEMA_VERSION]
    content_hash: str
    payload: dict[str, Any]

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        value = _normalize_text(value, "record content hash")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("record content hash must be lowercase sha256:<64 hex>")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "record payload")

    @model_validator(mode="after")
    def validate_typed_payload(self) -> RecordCreatedPayload:
        if self.record_type is RecordType.ARTIFACT:
            ArtifactDescriptor.model_validate(self.payload)
        return self


class RelationCreatedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relation_type: RelationType
    source_id: str
    target_id: str
    schema_version: Literal[RELATION_SCHEMA_VERSION]
    content_hash: str
    metadata: dict[str, Any]

    @field_validator("source_id", "target_id")
    @classmethod
    def normalize_ids(cls, value: str) -> str:
        value = _normalize_text(value, "relation endpoint id")
        if not value:
            raise ValueError("relation endpoint ids must not be empty")
        return value

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        value = _normalize_text(value, "relation content hash")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("relation content hash must be lowercase sha256:<64 hex>")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "relation metadata")


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    type: RecordType
    schema_version: Literal[RECORD_SCHEMA_VERSION]
    created_at: datetime
    created_by: Actor
    created_sequence: int = Field(ge=1)
    creating_event_id: str
    content_hash: str
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_event_identity(self) -> Record:
        if self.id != self.creating_event_id:
            raise ValueError("record id must equal its immutable creation event id")
        return self


class Relation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    type: RelationType
    source_id: str
    target_id: str
    schema_version: Literal[RELATION_SCHEMA_VERSION]
    created_at: datetime
    created_by: Actor
    created_sequence: int = Field(ge=1)
    creating_event_id: str
    content_hash: str
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def validate_event_identity(self) -> Relation:
        if self.id != self.creating_event_id:
            raise ValueError("relation id must equal its immutable creation event id")
        return self


def _content_hash(value: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def record_content_hash(draft: RecordDraft) -> str:
    return _content_hash(
        {
            "record_type": draft.record_type.value,
            "schema_version": draft.schema_version,
            "payload": draft.payload,
        }
    )


def relation_content_hash(draft: RelationDraft) -> str:
    return _content_hash(
        {
            "relation_type": draft.relation_type.value,
            "source_id": draft.source_id,
            "target_id": draft.target_id,
            "schema_version": draft.schema_version,
            "metadata": draft.metadata,
        }
    )


def record_event_payload(draft: RecordDraft) -> dict[str, Any]:
    return {
        "record_type": draft.record_type.value,
        "schema_version": draft.schema_version,
        "content_hash": record_content_hash(draft),
        "payload": draft.payload,
    }


def relation_event_payload(draft: RelationDraft) -> dict[str, Any]:
    return {
        "relation_type": draft.relation_type.value,
        "source_id": draft.source_id,
        "target_id": draft.target_id,
        "schema_version": draft.schema_version,
        "content_hash": relation_content_hash(draft),
        "metadata": draft.metadata,
    }


def record_from_event(event: LedgerEvent) -> Record:
    if event.event_type != RECORD_CREATED_EVENT_TYPE:
        raise ValueError(f"event {event.event_id} is not a record creation event")
    if event.stream_id != f"record:{event.event_id}":
        raise ValueError("record creation event stream does not match event/record id")

    payload = RecordCreatedPayload.model_validate(event.payload)
    expected_hash = _content_hash(
        {
            "record_type": payload.record_type.value,
            "schema_version": payload.schema_version,
            "payload": payload.payload,
        }
    )
    if payload.content_hash != expected_hash:
        raise ValueError("record content hash does not match canonical record content")

    return Record(
        id=event.event_id,
        type=payload.record_type,
        schema_version=payload.schema_version,
        created_at=event.occurred_at,
        created_by=event.actor,
        created_sequence=event.sequence,
        creating_event_id=event.event_id,
        content_hash=payload.content_hash,
        payload=payload.payload,
    )


def relation_from_event(event: LedgerEvent) -> Relation:
    if event.event_type != RELATION_CREATED_EVENT_TYPE:
        raise ValueError(f"event {event.event_id} is not a relation creation event")
    if event.stream_id != f"relation:{event.event_id}":
        raise ValueError("relation creation event stream does not match event/relation id")

    payload = RelationCreatedPayload.model_validate(event.payload)
    expected_hash = _content_hash(
        {
            "relation_type": payload.relation_type.value,
            "source_id": payload.source_id,
            "target_id": payload.target_id,
            "schema_version": payload.schema_version,
            "metadata": payload.metadata,
        }
    )
    if payload.content_hash != expected_hash:
        raise ValueError("relation content hash does not match canonical relation content")

    return Relation(
        id=event.event_id,
        type=payload.relation_type,
        source_id=payload.source_id,
        target_id=payload.target_id,
        schema_version=payload.schema_version,
        created_at=event.occurred_at,
        created_by=event.actor,
        created_sequence=event.sequence,
        creating_event_id=event.event_id,
        content_hash=payload.content_hash,
        metadata=payload.metadata,
    )
