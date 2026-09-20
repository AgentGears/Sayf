from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sayf.domain import Actor, ActorKind, LedgerEvent
from sayf.graph import CausalGraph, GraphDirection, GraphProjectionError
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RELATION_CREATED_EVENT_TYPE,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    record_event_payload,
    relation_event_payload,
)

ACTOR = Actor(kind=ActorKind.HUMAN, id="tester")
NOW = datetime(2026, 9, 20, tzinfo=UTC)


def event(sequence: int, event_id: str, event_type: str, stream: str, payload: dict) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id,
        sequence=sequence,
        stream_id=stream,
        event_type=event_type,
        occurred_at=NOW,
        actor=ACTOR,
        payload=payload,
        previous_event_hash=None,
        event_hash=f"sha256:{sequence:064x}",
    )


def record_event(sequence: int, record_id: str, record_type: RecordType) -> LedgerEvent:
    draft = RecordDraft(record_id=record_id, record_type=record_type, payload={"n": sequence})
    return event(
        sequence,
        record_id,
        RECORD_CREATED_EVENT_TYPE,
        f"record:{record_id}",
        record_event_payload(draft),
    )


def relation_event(
    sequence: int,
    relation_id: str,
    relation_type: RelationType,
    source: str,
    target: str,
) -> LedgerEvent:
    draft = RelationDraft(
        relation_id=relation_id,
        relation_type=relation_type,
        source_id=source,
        target_id=target,
        metadata={"n": sequence},
    )
    return event(
        sequence,
        relation_id,
        RELATION_CREATED_EVENT_TYPE,
        f"relation:{relation_id}",
        relation_event_payload(draft),
    )


def test_projection_and_directed_provenance_paths_are_deterministic() -> None:
    events = [
        record_event(1, "rec_a", RecordType.ASSUMPTION),
        record_event(2, "rec_b", RecordType.INTENT_REVISION),
        record_event(3, "rec_c", RecordType.TASK),
        relation_event(4, "rel_ab", RelationType.SUPPORTS, "rec_a", "rec_b"),
        relation_event(5, "rel_bc", RelationType.IMPLEMENTS, "rec_b", "rec_c"),
    ]
    graph = CausalGraph.from_events(events)

    neighbors = graph.neighbors("rec_a")
    assert [(n.record.id, n.relation.id) for n in neighbors] == [("rec_b", "rel_ab")]

    paths = graph.provenance_paths("rec_a", "rec_c")
    assert len(paths) == 1
    assert [step.relation.id for step in paths[0].steps] == ["rel_ab", "rel_bc"]
    assert [step.relation.creating_event_id for step in paths[0].steps] == [
        "rel_ab",
        "rel_bc",
    ]


def test_projection_fails_closed_on_missing_relation_endpoint() -> None:
    events = [
        record_event(1, "rec_a", RecordType.CLAIM),
        relation_event(2, "rel_bad", RelationType.SUPPORTS, "rec_a", "rec_missing"),
    ]
    with pytest.raises(GraphProjectionError, match="missing target record"):
        CausalGraph.from_events(events)


def test_projection_rejects_bad_reserved_payload_hash() -> None:
    created = record_event(1, "rec_a", RecordType.CLAIM)
    tampered = created.model_copy(
        update={"payload": {**created.payload, "content_hash": "sha256:" + "0" * 64}}
    )
    with pytest.raises(GraphProjectionError, match="content hash"):
        CausalGraph.from_events([tampered])


def test_projection_ignores_unknown_event_types() -> None:
    custom = event(1, "evt_custom", "custom.event", "custom", {"x": 1})
    created = record_event(2, "rec_a", RecordType.CLAIM)
    graph = CausalGraph.from_events([custom, created])
    assert [record.id for record in graph.records] == ["rec_a"]


def test_projection_requires_complete_contiguous_history() -> None:
    with pytest.raises(GraphProjectionError, match="complete contiguous"):
        CausalGraph.from_events(
            [
                event(2, "evt_2", "custom", "custom", {}),
                event(1, "evt_1", "custom", "custom", {}),
            ]
        )

    with pytest.raises(GraphProjectionError, match="expected sequence 2, found 3"):
        CausalGraph.from_events(
            [
                event(1, "evt_1", "custom", "custom", {}),
                event(3, "evt_3", "custom", "custom", {}),
            ]
        )


def test_bounded_cycle_safe_path_search() -> None:
    graph = CausalGraph.from_events(
        [
            record_event(1, "a", RecordType.CLAIM),
            record_event(2, "b", RecordType.CLAIM),
            record_event(3, "c", RecordType.CLAIM),
            relation_event(4, "ab", RelationType.SUPPORTS, "a", "b"),
            relation_event(5, "ba", RelationType.REFERENCES, "b", "a"),
            relation_event(6, "bc", RelationType.DERIVED_FROM, "b", "c"),
        ]
    )
    paths = graph.provenance_paths("a", "c", max_depth=3, max_paths=2)
    assert [[step.relation.id for step in path.steps] for path in paths] == [["ab", "bc"]]

    with pytest.raises(ValueError, match="max_depth"):
        graph.provenance_paths("a", "c", max_depth=33)
    with pytest.raises(ValueError, match="max_paths"):
        graph.provenance_paths("a", "c", max_paths=101)
    with pytest.raises(GraphProjectionError, match="expansion bound"):
        graph.provenance_paths("a", "c", max_expansions=1)


def test_both_direction_deduplicates_self_loop() -> None:
    graph = CausalGraph.from_events(
        [
            record_event(1, "a", RecordType.CLAIM),
            relation_event(2, "aa", RelationType.REFERENCES, "a", "a"),
        ]
    )
    neighbors = graph.neighbors("a", direction=GraphDirection.BOTH)
    assert len(neighbors) == 1


def test_artifact_record_payload_is_typed() -> None:
    with pytest.raises(ValidationError):
        RecordDraft(record_type=RecordType.ARTIFACT, payload={"digest": "bad"})
