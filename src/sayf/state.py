from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from sayf.domain import LedgerEvent, _normalize_text
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.records import Relation, RelationType

DEPENDENCY_BOUND_EVENT_TYPE = "sayf.dependency.bound.v1"
RECORD_INVALIDATED_EVENT_TYPE = "sayf.record.invalidated.v1"
RECORD_SUPERSEDED_EVENT_TYPE = "sayf.record.superseded.v1"
STATE_SCHEMA_VERSION = 1
STATE_EVENT_TYPES = frozenset(
    {
        DEPENDENCY_BOUND_EVENT_TYPE,
        RECORD_INVALIDATED_EVENT_TYPE,
        RECORD_SUPERSEDED_EVENT_TYPE,
    }
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class StateProjectionError(GraphProjectionError):
    """Raised when M0.3 revision/staleness history cannot be projected safely."""


class ValidityState(StrEnum):
    VALID = "valid"
    INVALIDATED = "invalidated"


class RevisionState(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


class FreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class StalenessRootKind(StrEnum):
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"


class SemanticRelationPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[STATE_SCHEMA_VERSION]
    relation_id: str
    relation_content_hash: str

    @field_validator("relation_id")
    @classmethod
    def normalize_relation_id(cls, value: str) -> str:
        value = _normalize_text(value, "state relation id")
        if not value:
            raise ValueError("state relation id must not be empty")
        return value

    @field_validator("relation_content_hash")
    @classmethod
    def validate_relation_content_hash(cls, value: str) -> str:
        value = _normalize_text(value, "state relation content hash")
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(
                "state relation content hash must be lowercase sha256:<64 hex>"
            )
        return value


class DependencyHop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relation_id: str
    binding_event_id: str
    dependent_id: str
    dependency_id: str


class StalenessCause(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root_record_id: str
    root_kind: StalenessRootKind
    dependency_path: tuple[DependencyHop, ...]


class RecordEffectiveState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str
    validity: ValidityState
    revision: RevisionState
    freshness: FreshnessState
    invalidated_by_record_ids: tuple[str, ...] = ()
    invalidation_relation_ids: tuple[str, ...] = ()
    invalidation_event_ids: tuple[str, ...] = ()
    superseded_by_record_id: str | None = None
    supersession_relation_id: str | None = None
    supersession_event_id: str | None = None
    stale_causes: tuple[StalenessCause, ...] = ()


@dataclass(frozen=True)
class _Activation:
    event_id: str
    sequence: int
    relation: Relation


def semantic_relation_event_payload(relation: Relation) -> dict[str, object]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "relation_id": relation.id,
        "relation_content_hash": relation.content_hash,
    }


class EffectiveStateProjection:
    """Derived M0.3 effective state over immutable M0.1/M0.2 history."""

    def __init__(
        self,
        *,
        graph: CausalGraph,
        bound_dependencies: tuple[_Activation, ...],
        invalidations: tuple[_Activation, ...],
        supersessions: tuple[_Activation, ...],
    ) -> None:
        self._graph = graph
        self._bound_dependencies = bound_dependencies
        self._invalidations = invalidations
        self._supersessions = supersessions
        self._activated_relation_ids = frozenset(
            activation.relation.id
            for activation in (*bound_dependencies, *invalidations, *supersessions)
        )
        self._superseded_by = {
            activation.relation.target_id: activation for activation in supersessions
        }
        self._supersedes_target_by_source = {
            activation.relation.source_id: activation for activation in supersessions
        }
        self._states = self._derive_states()

    @classmethod
    def from_events(
        cls,
        events: Iterable[LedgerEvent],
        *,
        graph: CausalGraph | None = None,
    ) -> EffectiveStateProjection:
        event_list = tuple(events)
        graph = CausalGraph.from_events(event_list) if graph is None else graph

        bound_dependencies: list[_Activation] = []
        invalidations: list[_Activation] = []
        supersessions: list[_Activation] = []
        activated_relation_ids: set[str] = set()
        superseded_by: dict[str, _Activation] = {}
        supersedes_target_by_source: dict[str, _Activation] = {}

        for event in event_list:
            expected_relation_type: RelationType | None = None
            target_list: list[_Activation] | None = None
            if event.event_type == DEPENDENCY_BOUND_EVENT_TYPE:
                expected_relation_type = RelationType.DEPENDS_ON
                target_list = bound_dependencies
            elif event.event_type == RECORD_INVALIDATED_EVENT_TYPE:
                expected_relation_type = RelationType.INVALIDATES
                target_list = invalidations
            elif event.event_type == RECORD_SUPERSEDED_EVENT_TYPE:
                expected_relation_type = RelationType.SUPERSEDES
                target_list = supersessions
            else:
                continue

            try:
                payload = SemanticRelationPayload.model_validate(event.payload)
                if event.stream_id != f"state:{payload.relation_id}":
                    raise StateProjectionError(
                        "state event stream does not match the qualified relation id"
                    )

                relation = graph.relation(payload.relation_id)
                if relation.created_sequence >= event.sequence:
                    raise StateProjectionError(
                        f"state event {event.event_id} references relation "
                        f"{relation.id} before that relation exists"
                    )
                if relation.content_hash != payload.relation_content_hash:
                    raise StateProjectionError(
                        f"state event {event.event_id} does not bind the current "
                        f"immutable content hash of relation {relation.id}"
                    )
                if relation.type is not expected_relation_type:
                    raise StateProjectionError(
                        f"state event {event.event_id} requires relation type "
                        f"{expected_relation_type.value}, found {relation.type.value}"
                    )
                if relation.id in activated_relation_ids:
                    raise StateProjectionError(
                        f"relation {relation.id} has more than one M0.3 state activation"
                    )
                if relation.source_id == relation.target_id:
                    raise StateProjectionError(
                        f"state relation {relation.id} must not be a self-loop"
                    )

                activation = _Activation(
                    event_id=event.event_id,
                    sequence=event.sequence,
                    relation=relation,
                )

                if expected_relation_type is RelationType.SUPERSEDES:
                    cls._validate_supersession_activation(
                        graph,
                        activation,
                        superseded_by=superseded_by,
                        supersedes_target_by_source=supersedes_target_by_source,
                    )
                    superseded_by[relation.target_id] = activation
                    supersedes_target_by_source[relation.source_id] = activation

                activated_relation_ids.add(relation.id)
                assert target_list is not None
                target_list.append(activation)
            except StateProjectionError:
                raise
            except GraphProjectionError as exc:
                raise StateProjectionError(
                    f"invalid M0.3 state event at ledger sequence {event.sequence} "
                    f"({event.event_id}): {exc}"
                ) from exc
            except (
                ValidationError,
                TypeError,
                ValueError,
                OverflowError,
                RecursionError,
            ) as exc:
                raise StateProjectionError(
                    f"invalid M0.3 state event at ledger sequence {event.sequence} "
                    f"({event.event_id}): {exc}"
                ) from exc

        return cls(
            graph=graph,
            bound_dependencies=tuple(bound_dependencies),
            invalidations=tuple(invalidations),
            supersessions=tuple(supersessions),
        )

    @staticmethod
    def _validate_supersession_activation(
        graph: CausalGraph,
        activation: _Activation,
        *,
        superseded_by: dict[str, _Activation],
        supersedes_target_by_source: dict[str, _Activation],
    ) -> None:
        relation = activation.relation
        source = graph.record(relation.source_id)
        target = graph.record(relation.target_id)
        if source.type is not target.type:
            raise StateProjectionError(
                f"supersession relation {relation.id} must connect records of the same type"
            )
        if relation.target_id in superseded_by:
            existing = superseded_by[relation.target_id]
            raise StateProjectionError(
                f"record {relation.target_id} is already superseded through relation "
                f"{existing.relation.id}"
            )
        if relation.source_id in supersedes_target_by_source:
            existing = supersedes_target_by_source[relation.source_id]
            raise StateProjectionError(
                f"record {relation.source_id} already supersedes record "
                f"{existing.relation.target_id}"
            )
        if relation.source_id in superseded_by:
            existing = superseded_by[relation.source_id]
            raise StateProjectionError(
                f"record {relation.source_id} was already superseded by "
                f"{existing.relation.source_id} before it could supersede another revision"
            )

    def _derive_states(self) -> dict[str, RecordEffectiveState]:
        invalidations_by_target: dict[str, list[_Activation]] = defaultdict(list)
        for activation in self._invalidations:
            invalidations_by_target[activation.relation.target_id].append(activation)

        reverse_dependencies: dict[str, list[_Activation]] = defaultdict(list)
        for activation in self._bound_dependencies:
            reverse_dependencies[activation.relation.target_id].append(activation)
        for activations in reverse_dependencies.values():
            activations.sort(key=lambda item: (item.sequence, item.relation.id))

        root_seeds: list[tuple[int, str, StalenessRootKind]] = []
        for target_id, activations in invalidations_by_target.items():
            first_sequence = min(activation.sequence for activation in activations)
            root_seeds.append(
                (first_sequence, target_id, StalenessRootKind.INVALIDATED)
            )
        for target_id, activation in self._superseded_by.items():
            root_seeds.append(
                (activation.sequence, target_id, StalenessRootKind.SUPERSEDED)
            )
        root_seeds.sort(key=lambda item: (item[0], item[1], item[2].value))

        causes_by_record: dict[str, list[StalenessCause]] = defaultdict(list)
        for _, root_id, root_kind in root_seeds:
            queue: deque[tuple[str, tuple[DependencyHop, ...]]] = deque()
            queue.append((root_id, ()))
            visited = {root_id}

            while queue:
                dependency_id, path_to_root = queue.popleft()
                for activation in reverse_dependencies.get(dependency_id, ()):
                    dependent_id = activation.relation.source_id
                    if dependent_id in visited:
                        continue
                    visited.add(dependent_id)
                    hop = DependencyHop(
                        relation_id=activation.relation.id,
                        binding_event_id=activation.event_id,
                        dependent_id=dependent_id,
                        dependency_id=dependency_id,
                    )
                    dependency_path = (hop, *path_to_root)
                    causes_by_record[dependent_id].append(
                        StalenessCause(
                            root_record_id=root_id,
                            root_kind=root_kind,
                            dependency_path=dependency_path,
                        )
                    )
                    queue.append((dependent_id, dependency_path))

        states: dict[str, RecordEffectiveState] = {}
        for record in self._graph.records:
            invalidations = sorted(
                invalidations_by_target.get(record.id, ()),
                key=lambda item: (item.sequence, item.relation.id),
            )
            supersession = self._superseded_by.get(record.id)
            stale_causes = tuple(causes_by_record.get(record.id, ()))
            states[record.id] = RecordEffectiveState(
                record_id=record.id,
                validity=(
                    ValidityState.INVALIDATED if invalidations else ValidityState.VALID
                ),
                revision=(
                    RevisionState.SUPERSEDED
                    if supersession is not None
                    else RevisionState.CURRENT
                ),
                freshness=(
                    FreshnessState.STALE if stale_causes else FreshnessState.FRESH
                ),
                invalidated_by_record_ids=tuple(
                    item.relation.source_id for item in invalidations
                ),
                invalidation_relation_ids=tuple(
                    item.relation.id for item in invalidations
                ),
                invalidation_event_ids=tuple(item.event_id for item in invalidations),
                superseded_by_record_id=(
                    supersession.relation.source_id
                    if supersession is not None
                    else None
                ),
                supersession_relation_id=(
                    supersession.relation.id if supersession is not None else None
                ),
                supersession_event_id=(
                    supersession.event_id if supersession is not None else None
                ),
                stale_causes=stale_causes,
            )
        return states

    @property
    def states(self) -> tuple[RecordEffectiveState, ...]:
        return tuple(self._states[record.id] for record in self._graph.records)

    def state(self, record_id: str) -> RecordEffectiveState:
        self._graph.record(record_id)
        return self._states[record_id]

    def affected(self, root_record_id: str) -> tuple[RecordEffectiveState, ...]:
        self._graph.record(root_record_id)
        return tuple(
            state
            for state in self.states
            if any(
                cause.root_record_id == root_record_id for cause in state.stale_causes
            )
        )

    def _relation_for_activation(
        self,
        relation_id: str,
        *,
        expected_type: RelationType,
    ) -> Relation:
        relation = self._graph.relation(relation_id)
        if relation.type is not expected_type:
            raise StateProjectionError(
                f"relation {relation.id} must have type {expected_type.value}, "
                f"found {relation.type.value}"
            )
        if relation.id in self._activated_relation_ids:
            raise StateProjectionError(
                f"relation {relation.id} already has an M0.3 state activation"
            )
        if relation.source_id == relation.target_id:
            raise StateProjectionError(
                f"state relation {relation.id} must not be a self-loop"
            )
        return relation

    def validate_dependency_binding(self, relation_id: str) -> Relation:
        return self._relation_for_activation(
            relation_id,
            expected_type=RelationType.DEPENDS_ON,
        )

    def validate_invalidation(self, relation_id: str) -> Relation:
        return self._relation_for_activation(
            relation_id,
            expected_type=RelationType.INVALIDATES,
        )

    def validate_supersession(self, relation_id: str) -> Relation:
        relation = self._relation_for_activation(
            relation_id,
            expected_type=RelationType.SUPERSEDES,
        )
        self._validate_supersession_activation(
            self._graph,
            _Activation(event_id="<pending>", sequence=0, relation=relation),
            superseded_by=self._superseded_by,
            supersedes_target_by_source=self._supersedes_target_by_source,
        )
        return relation
