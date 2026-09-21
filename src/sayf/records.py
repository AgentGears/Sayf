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


M04_SEMANTIC_RECORD_TYPES = frozenset(
    {
        RecordType.VERIFICATION_RECEIPT,
        RecordType.POLICY_SNAPSHOT,
        RecordType.GATE_REQUEST,
        RecordType.GATE_DECISION,
    }
)

M05_SEMANTIC_RECORD_TYPES = frozenset(
    {
        RecordType.RELEASE,
        RecordType.RUNTIME_OBSERVATION,
        RecordType.FEEDBACK_CASE,
    }
)

SEMANTIC_RECORD_TYPES = M04_SEMANTIC_RECORD_TYPES | M05_SEMANTIC_RECORD_TYPES

# RiskAssessment remains intentionally unavailable after M0.5. Persisting one through
# the generic record surface would freeze authoritative-looking state before its
# contract and policy semantics are defined.
RESERVED_CONTRACT_RECORD_TYPES = frozenset({RecordType.RISK_ASSESSMENT})


class VerificationResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    INCONCLUSIVE = "inconclusive"


class VerificationIndependence(StrEnum):
    YES = "yes"
    NO = "no"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"


class GateOutcome(StrEnum):
    PERMIT = "permit"
    BLOCK = "block"


class RuntimeOutcome(StrEnum):
    NOMINAL = "nominal"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FeedbackClassification(StrEnum):
    FALSIFIES = "falsifies"
    CONTRADICTS = "contradicts"
    VIOLATES = "violates"
    ANOMALY = "anomaly"
    INFORMATIONAL = "informational"


class FeedbackEffect(StrEnum):
    NONE = "none"
    INVALIDATE_TARGET = "invalidate_target"


class RecordBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str = Field(min_length=1)
    content_hash: str

    @field_validator("record_id")
    @classmethod
    def normalize_record_id(cls, value: str) -> str:
        return _normalize_text(value, "record binding id")

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        value = _normalize_text(value, "record binding content hash")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("record binding hash must be lowercase sha256:<64 hex>")
        return value


class VerificationRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str = Field(min_length=1)
    minimum_passes: int = Field(default=1, ge=1)

    @field_validator("contract")
    @classmethod
    def normalize_contract(cls, value: str) -> str:
        return _normalize_text(value, "verification contract")


class VerificationReceiptPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: RecordBinding
    contract: str = Field(min_length=1)
    result: VerificationResult
    evidence: tuple[RecordBinding, ...] = Field(min_length=1)
    environment: dict[str, Any] = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    independent_from_generation: VerificationIndependence
    verified_claim: str | None = None
    prohibited_generalizations: tuple[str, ...] = ()

    @field_validator("contract")
    @classmethod
    def normalize_contract(cls, value: str) -> str:
        return _normalize_text(value, "verification contract")

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "verification environment")

    @field_validator("limitations", "prohibited_generalizations")
    @classmethod
    def normalize_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_normalize_text(item, "verification text") for item in value)

    @field_validator("verified_claim")
    @classmethod
    def normalize_verified_claim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _normalize_text(value, "verified claim")
        if not value:
            raise ValueError("verified claim must not be empty")
        return value

    @model_validator(mode="after")
    def validate_receipt_semantics(self) -> VerificationReceiptPayload:
        evidence_ids = [binding.record_id for binding in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("verification evidence bindings must be unique by record id")
        if self.result is VerificationResult.PASS and self.verified_claim is None:
            raise ValueError("pass verification requires a bounded verified_claim")
        if self.result in {VerificationResult.FAIL, VerificationResult.INCONCLUSIVE}:
            if self.verified_claim is not None:
                raise ValueError(
                    "fail/inconclusive verification must not assert a verified_claim"
                )
        return self


class PolicySnapshotPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    requirements: tuple[VerificationRequirement, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_text(value, "policy snapshot name")

    @model_validator(mode="after")
    def validate_unique_contracts(self) -> PolicySnapshotPayload:
        contracts = [requirement.contract for requirement in self.requirements]
        if len(contracts) != len(set(contracts)):
            raise ValueError("policy verification contracts must be unique")
        return self


class GateRequestPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: RecordBinding
    policy_snapshot: RecordBinding
    verification_receipts: tuple[RecordBinding, ...] = ()

    @model_validator(mode="after")
    def validate_unique_receipts(self) -> GateRequestPayload:
        receipt_ids = [binding.record_id for binding in self.verification_receipts]
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("gate request receipt bindings must be unique by record id")
        return self


class GateInputBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str = Field(min_length=1)
    content_hash: str
    effective_state_hash: str

    @field_validator("record_id")
    @classmethod
    def normalize_record_id(cls, value: str) -> str:
        return _normalize_text(value, "gate input record id")

    @field_validator("content_hash", "effective_state_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        value = _normalize_text(value, "gate input hash")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("gate input hashes must be lowercase sha256:<64 hex>")
        return value


class GateDecisionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_request: RecordBinding
    subject: RecordBinding
    policy_snapshot: RecordBinding
    verification_receipts: tuple[RecordBinding, ...]
    evaluated_inputs: tuple[GateInputBinding, ...] = Field(min_length=3)
    outcome: GateOutcome
    reasons: tuple[str, ...] = ()
    satisfied_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()

    @field_validator("reasons", "satisfied_requirements", "missing_requirements")
    @classmethod
    def normalize_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_normalize_text(item, "gate decision text") for item in value)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> GateDecisionPayload:
        receipt_ids = [binding.record_id for binding in self.verification_receipts]
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("gate decision receipt bindings must be unique by record id")
        input_ids = [binding.record_id for binding in self.evaluated_inputs]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("gate evaluated inputs must be unique by record id")
        if self.outcome is GateOutcome.PERMIT and self.reasons:
            raise ValueError("permit gate decision must not contain blocking reasons")
        if self.outcome is GateOutcome.PERMIT and self.missing_requirements:
            raise ValueError("permit gate decision must not have missing requirements")
        if self.outcome is GateOutcome.BLOCK and not self.reasons:
            raise ValueError("block gate decision requires at least one reason")
        return self


class ReleasePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_ref: str = Field(min_length=1)
    subject: RecordBinding
    gate_decision: RecordBinding
    authority_fingerprint_version: Literal[1] = 1
    authority_fingerprint: str
    environment: dict[str, Any] = Field(min_length=1)

    @field_validator("release_ref")
    @classmethod
    def normalize_release_ref(cls, value: str) -> str:
        return _normalize_text(value, "release ref")

    @field_validator("authority_fingerprint")
    @classmethod
    def validate_authority_fingerprint(cls, value: str) -> str:
        value = _normalize_text(value, "release authority fingerprint")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(
                "release authority fingerprint must be lowercase sha256:<64 hex>"
            )
        return value

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "release environment")


class RuntimeObservationPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release: RecordBinding
    outcome: RuntimeOutcome
    summary: str = Field(min_length=1)
    environment: dict[str, Any] = Field(min_length=1)
    evidence: tuple[RecordBinding, ...] = ()

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        return _normalize_text(value, "runtime observation summary")

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, Any]:
        return _normalize_json_object(value, "runtime observation environment")

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> RuntimeObservationPayload:
        ids = [binding.record_id for binding in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("runtime observation evidence must be unique by record id")
        return self


class FeedbackCasePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observation: RecordBinding
    target: RecordBinding
    classification: FeedbackClassification
    proposed_effect: FeedbackEffect = FeedbackEffect.NONE
    rationale: str = Field(min_length=1)

    @field_validator("rationale")
    @classmethod
    def normalize_rationale(cls, value: str) -> str:
        return _normalize_text(value, "feedback rationale")

    @model_validator(mode="after")
    def validate_effect(self) -> FeedbackCasePayload:
        if self.proposed_effect is FeedbackEffect.INVALIDATE_TARGET:
            if self.classification not in {
                FeedbackClassification.FALSIFIES,
                FeedbackClassification.CONTRADICTS,
                FeedbackClassification.VIOLATES,
            }:
                raise ValueError(
                    "target invalidation requires falsifies, contradicts, or violates "
                    "classification"
                )
        return self


_SEMANTIC_PAYLOAD_MODELS: dict[RecordType, type[BaseModel]] = {
    RecordType.VERIFICATION_RECEIPT: VerificationReceiptPayload,
    RecordType.POLICY_SNAPSHOT: PolicySnapshotPayload,
    RecordType.GATE_REQUEST: GateRequestPayload,
    RecordType.GATE_DECISION: GateDecisionPayload,
    RecordType.RELEASE: ReleasePayload,
    RecordType.RUNTIME_OBSERVATION: RuntimeObservationPayload,
    RecordType.FEEDBACK_CASE: FeedbackCasePayload,
}


def _validate_semantic_payload(record_type: RecordType, payload: dict[str, Any]) -> dict[str, Any]:
    model_type = _SEMANTIC_PAYLOAD_MODELS.get(record_type)
    if model_type is None:
        return payload
    return model_type.model_validate(payload).model_dump(mode="json")


def _ensure_record_type_contract_implemented(record_type: RecordType) -> None:
    if record_type in RESERVED_CONTRACT_RECORD_TYPES:
        raise ValueError(
            f"record type {record_type.value} is reserved until its semantic contract "
            "is implemented"
        )


def _ensure_generic_record_creation_allowed(record_type: RecordType) -> None:
    _ensure_record_type_contract_implemented(record_type)
    if record_type in M04_SEMANTIC_RECORD_TYPES:
        raise ValueError(
            f"record type {record_type.value} must be created through its M0.4 "
            "semantic API"
        )
    if record_type in M05_SEMANTIC_RECORD_TYPES:
        raise ValueError(
            f"record type {record_type.value} must be created through its M0.5 "
            "semantic API"
        )


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
        _ensure_generic_record_creation_allowed(self.record_type)
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
        _ensure_record_type_contract_implemented(self.record_type)
        if self.record_type is RecordType.ARTIFACT:
            ArtifactDescriptor.model_validate(self.payload)
        elif self.record_type in SEMANTIC_RECORD_TYPES:
            _validate_semantic_payload(self.record_type, self.payload)
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


def semantic_record_event_payload(
    record_type: RecordType,
    payload: BaseModel | dict[str, Any],
) -> dict[str, Any]:
    if record_type not in SEMANTIC_RECORD_TYPES:
        raise ValueError(f"record type {record_type.value} is not a semantic record")
    payload_value = (
        payload.model_dump(mode="json")
        if isinstance(payload, BaseModel)
        else _normalize_json_object(payload, "semantic record payload")
    )
    payload_value = _validate_semantic_payload(record_type, payload_value)
    return {
        "record_type": record_type.value,
        "schema_version": RECORD_SCHEMA_VERSION,
        "content_hash": _content_hash(
            {
                "record_type": record_type.value,
                "schema_version": RECORD_SCHEMA_VERSION,
                "payload": payload_value,
            }
        ),
        "payload": payload_value,
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
    normalized_payload = (
        _validate_semantic_payload(payload.record_type, payload.payload)
        if payload.record_type in SEMANTIC_RECORD_TYPES
        else payload.payload
    )
    expected_hash = _content_hash(
        {
            "record_type": payload.record_type.value,
            "schema_version": payload.schema_version,
            "payload": normalized_payload,
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
        payload=normalized_payload,
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
