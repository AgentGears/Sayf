from __future__ import annotations

from pathlib import Path

import pytest

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.records import (
    RELATION_CREATED_EVENT_TYPE,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    relation_event_payload,
)
from sayf.state import DEPENDENCY_BOUND_EVENT_TYPE, FreshnessState, StateProjectionError

ACTOR = Actor(kind=ActorKind.HUMAN, id="m03-edge-test")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def record(repo: CausalRepository, record_id: str, record_type: RecordType) -> None:
    repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type),
        actor=ACTOR,
    )


def relation(
    repo: CausalRepository,
    relation_id: str,
    relation_type: RelationType,
    source_id: str,
    target_id: str,
) -> None:
    repo.create_relation(
        RelationDraft(
            relation_id=relation_id,
            relation_type=relation_type,
            source_id=source_id,
            target_id=target_id,
        ),
        actor=ACTOR,
    )


def test_state_activation_rejects_self_loop_relation(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record(repo, "a1", RecordType.ASSUMPTION)
    relation(repo, "dep_self", RelationType.DEPENDS_ON, "a1", "a1")

    with pytest.raises(StateProjectionError, match="must not be a self-loop"):
        repo.bind_dependency("dep_self", actor=ACTOR)


def test_state_projection_rejects_forward_relation_reference(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record(repo, "i1", RecordType.INTENT_REVISION)
    record(repo, "a1", RecordType.ASSUMPTION)

    draft = RelationDraft(
        relation_id="dep_future",
        relation_type=RelationType.DEPENDS_ON,
        source_id="i1",
        target_id="a1",
    )
    future_relation_payload = relation_event_payload(draft)
    repo.event_store.append(
        EventDraft(
            stream_id="state:dep_future",
            event_type=DEPENDENCY_BOUND_EVENT_TYPE,
            actor=ACTOR,
            payload={
                "schema_version": 1,
                "relation_id": "dep_future",
                "relation_content_hash": future_relation_payload["content_hash"],
            },
        )
    )
    repo.event_store.append(
        EventDraft(
            event_id="dep_future",
            stream_id="relation:dep_future",
            event_type=RELATION_CREATED_EVENT_TYPE,
            actor=ACTOR,
            payload=future_relation_payload,
        )
    )

    with pytest.raises(StateProjectionError, match="before that relation exists"):
        repo.effective_state()


def test_supersession_source_cannot_directly_supersede_two_records(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    for record_id in ("i0", "i1", "i2"):
        record(repo, record_id, RecordType.INTENT_REVISION)

    relation(repo, "sup_i2_i1", RelationType.SUPERSEDES, "i2", "i1")
    relation(repo, "sup_i2_i0", RelationType.SUPERSEDES, "i2", "i0")
    repo.supersede("sup_i2_i1", actor=ACTOR)

    with pytest.raises(StateProjectionError, match="already supersedes record i1"):
        repo.supersede("sup_i2_i0", actor=ACTOR)


def test_older_record_cannot_supersede_newer_record(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record(repo, "older", RecordType.INTENT_REVISION)
    record(repo, "newer", RecordType.INTENT_REVISION)
    relation(repo, "sup_backwards", RelationType.SUPERSEDES, "older", "newer")

    with pytest.raises(StateProjectionError, match="must be created after"):
        repo.supersede("sup_backwards", actor=ACTOR)


def test_multiple_invalidation_causes_preserve_activation_order(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record(repo, "a1", RecordType.ASSUMPTION)
    record(repo, "o1", RecordType.OBSERVATION)
    record(repo, "o2", RecordType.OBSERVATION)
    relation(repo, "inv_o1", RelationType.INVALIDATES, "o1", "a1")
    relation(repo, "inv_o2", RelationType.INVALIDATES, "o2", "a1")

    repo.invalidate("inv_o2", actor=ACTOR)
    repo.invalidate("inv_o1", actor=ACTOR)

    state = repo.effective_state().state("a1")
    assert state.invalidated_by_record_ids == ("o2", "o1")
    assert state.invalidation_relation_ids == ("inv_o2", "inv_o1")


def test_dependency_cycle_does_not_mark_root_stale_from_itself(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record(repo, "a", RecordType.ASSUMPTION)
    record(repo, "b", RecordType.CLAIM)
    record(repo, "o", RecordType.OBSERVATION)
    relation(repo, "dep_a_b", RelationType.DEPENDS_ON, "a", "b")
    relation(repo, "dep_b_a", RelationType.DEPENDS_ON, "b", "a")
    relation(repo, "inv_o_a", RelationType.INVALIDATES, "o", "a")

    repo.bind_dependency("dep_a_b", actor=ACTOR)
    repo.bind_dependency("dep_b_a", actor=ACTOR)
    repo.invalidate("inv_o_a", actor=ACTOR)

    state = repo.effective_state()
    assert state.state("a").freshness is FreshnessState.FRESH
    assert state.state("b").freshness is FreshnessState.STALE
    assert [item.record_id for item in state.affected("a")] == ["b"]


def test_dependency_bound_after_invalidation_becomes_stale_immediately(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    record(repo, "a1", RecordType.ASSUMPTION)
    record(repo, "i1", RecordType.INTENT_REVISION)
    record(repo, "o1", RecordType.OBSERVATION)
    relation(repo, "dep_i1_a1", RelationType.DEPENDS_ON, "i1", "a1")
    relation(repo, "inv_o1_a1", RelationType.INVALIDATES, "o1", "a1")

    repo.invalidate("inv_o1_a1", actor=ACTOR)
    assert repo.effective_state().state("i1").freshness is FreshnessState.FRESH

    repo.bind_dependency("dep_i1_a1", actor=ACTOR)
    assert repo.effective_state().state("i1").freshness is FreshnessState.STALE
