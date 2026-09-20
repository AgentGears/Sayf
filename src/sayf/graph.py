from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from sayf.domain import LedgerEvent
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RELATION_CREATED_EVENT_TYPE,
    Record,
    Relation,
    RelationType,
    record_from_event,
    relation_from_event,
)


class GraphProjectionError(RuntimeError):
    """Raised when typed semantic history cannot be projected deterministically."""


class GraphDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
    BOTH = "both"


class GraphNeighbor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record: Record
    relation: Relation
    direction: GraphDirection


class GraphPathStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: Relation
    from_id: str
    to_id: str
    direction: GraphDirection


class GraphPath(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_id: str
    end_id: str
    steps: tuple[GraphPathStep, ...]

    @property
    def depth(self) -> int:
        return len(self.steps)


class CausalGraph:
    """Deterministic in-memory projection of complete authoritative Sayf history."""

    def __init__(
        self,
        records: dict[str, Record],
        relations: dict[str, Relation],
        outgoing: dict[str, tuple[str, ...]],
        incoming: dict[str, tuple[str, ...]],
    ) -> None:
        self._records = records
        self._relations = relations
        self._outgoing = outgoing
        self._incoming = incoming

    @classmethod
    def from_events(cls, events: Iterable[LedgerEvent]) -> CausalGraph:
        records: dict[str, Record] = {}
        relations: dict[str, Relation] = {}
        outgoing_lists: dict[str, list[str]] = defaultdict(list)
        incoming_lists: dict[str, list[str]] = defaultdict(list)

        expected_sequence = 1
        for event in events:
            if event.sequence != expected_sequence:
                raise GraphProjectionError(
                    "typed projection requires complete contiguous ledger history: "
                    f"expected sequence {expected_sequence}, found {event.sequence}"
                )
            expected_sequence += 1

            try:
                if event.event_type == RECORD_CREATED_EVENT_TYPE:
                    record = record_from_event(event)
                    if record.id in records:
                        raise GraphProjectionError(f"duplicate record id {record.id}")
                    records[record.id] = record
                    continue

                if event.event_type == RELATION_CREATED_EVENT_TYPE:
                    relation = relation_from_event(event)
                    if relation.id in relations:
                        raise GraphProjectionError(f"duplicate relation id {relation.id}")
                    if relation.source_id not in records:
                        raise GraphProjectionError(
                            f"relation {relation.id} references missing source record "
                            f"{relation.source_id}"
                        )
                    if relation.target_id not in records:
                        raise GraphProjectionError(
                            f"relation {relation.id} references missing target record "
                            f"{relation.target_id}"
                        )
                    relations[relation.id] = relation
                    outgoing_lists[relation.source_id].append(relation.id)
                    incoming_lists[relation.target_id].append(relation.id)
            except GraphProjectionError:
                raise
            except (ValidationError, TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise GraphProjectionError(
                    f"invalid typed event at ledger sequence {event.sequence} "
                    f"({event.event_id}): {exc}"
                ) from exc

        def relation_order(relation_id: str) -> tuple[int, str]:
            relation = relations[relation_id]
            return (relation.created_sequence, relation.id)

        outgoing = {
            record_id: tuple(sorted(relation_ids, key=relation_order))
            for record_id, relation_ids in outgoing_lists.items()
        }
        incoming = {
            record_id: tuple(sorted(relation_ids, key=relation_order))
            for record_id, relation_ids in incoming_lists.items()
        }
        return cls(records=records, relations=relations, outgoing=outgoing, incoming=incoming)

    @property
    def records(self) -> tuple[Record, ...]:
        return tuple(
            sorted(
                self._records.values(),
                key=lambda item: (item.created_sequence, item.id),
            )
        )

    @property
    def relations(self) -> tuple[Relation, ...]:
        return tuple(
            sorted(self._relations.values(), key=lambda item: (item.created_sequence, item.id))
        )

    def record(self, record_id: str) -> Record:
        try:
            return self._records[record_id]
        except KeyError as exc:
            raise GraphProjectionError(f"unknown record id {record_id}") from exc

    def relation(self, relation_id: str) -> Relation:
        try:
            return self._relations[relation_id]
        except KeyError as exc:
            raise GraphProjectionError(f"unknown relation id {relation_id}") from exc

    def neighbors(
        self,
        record_id: str,
        *,
        direction: GraphDirection = GraphDirection.OUTBOUND,
        relation_types: set[RelationType] | None = None,
    ) -> tuple[GraphNeighbor, ...]:
        self.record(record_id)
        result: list[GraphNeighbor] = []

        if direction in (GraphDirection.OUTBOUND, GraphDirection.BOTH):
            for relation_id in self._outgoing.get(record_id, ()):
                relation = self._relations[relation_id]
                if relation_types is not None and relation.type not in relation_types:
                    continue
                result.append(
                    GraphNeighbor(
                        record=self._records[relation.target_id],
                        relation=relation,
                        direction=GraphDirection.OUTBOUND,
                    )
                )

        if direction in (GraphDirection.INBOUND, GraphDirection.BOTH):
            for relation_id in self._incoming.get(record_id, ()):
                relation = self._relations[relation_id]
                if relation_types is not None and relation.type not in relation_types:
                    continue
                if relation.source_id == record_id and relation.target_id == record_id:
                    if direction is GraphDirection.BOTH:
                        continue
                result.append(
                    GraphNeighbor(
                        record=self._records[relation.source_id],
                        relation=relation,
                        direction=GraphDirection.INBOUND,
                    )
                )

        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.relation.created_sequence,
                    item.relation.id,
                    item.direction.value,
                ),
            )
        )

    def provenance_paths(
        self,
        start_id: str,
        end_id: str,
        *,
        direction: GraphDirection = GraphDirection.OUTBOUND,
        relation_types: set[RelationType] | None = None,
        max_depth: int = 8,
        max_paths: int = 20,
        max_expansions: int = 10_000,
    ) -> tuple[GraphPath, ...]:
        if not 0 <= max_depth <= 32:
            raise ValueError("max_depth must be between 0 and 32")
        if not 1 <= max_paths <= 100:
            raise ValueError("max_paths must be between 1 and 100")
        if not 1 <= max_expansions <= 100_000:
            raise ValueError("max_expansions must be between 1 and 100000")

        self.record(start_id)
        self.record(end_id)
        if start_id == end_id:
            return (GraphPath(start_id=start_id, end_id=end_id, steps=()),)
        if max_depth == 0:
            return ()

        queue: deque[tuple[str, tuple[GraphPathStep, ...], frozenset[str]]] = deque()
        queue.append((start_id, (), frozenset({start_id})))
        found: list[GraphPath] = []
        expansions = 0

        while queue:
            current_id, steps, visited = queue.popleft()
            if len(steps) >= max_depth:
                continue

            for neighbor in self.neighbors(
                current_id,
                direction=direction,
                relation_types=relation_types,
            ):
                expansions += 1
                if expansions > max_expansions:
                    raise GraphProjectionError(
                        "provenance path search exceeded the configured expansion bound"
                    )
                next_id = neighbor.record.id
                if next_id in visited:
                    continue
                step = GraphPathStep(
                    relation=neighbor.relation,
                    from_id=current_id,
                    to_id=next_id,
                    direction=neighbor.direction,
                )
                next_steps = (*steps, step)
                if next_id == end_id:
                    found.append(
                        GraphPath(start_id=start_id, end_id=end_id, steps=next_steps)
                    )
                    if len(found) > max_paths:
                        raise GraphProjectionError(
                            "provenance path search exceeded the configured result bound"
                        )
                    continue
                queue.append((next_id, next_steps, visited | {next_id}))

        return tuple(found)
