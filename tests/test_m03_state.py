from __future__ import annotations

from pathlib import Path

import pytest

import sayf.causal as causal_module
from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.records import RecordDraft, RecordType, RelationDraft, RelationType
from sayf.state import (
    RECORD_INVALIDATED_EVENT_TYPE,
    FreshnessState,
    RevisionState,
    StalenessRootKind,
    StateProjectionError,
    ValidityState,
    semantic_relation_event_payload,
)
from sayf.storage import LedgerReadError, SQLiteEventStore

ACTOR = Actor(kind=ActorKind.HUMAN, id="m03-test")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create_record(
    repo: CausalRepository,
    record_id: str,
    record_type: RecordType,
) -> None:
    repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type),
        actor=ACTOR,
    )


def create_relation(
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


def test_unqualified_m02_relations_do_not_gain_m03_authority(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "a1", RecordType.ASSUMPTION)
    create_record(repo, "i1", RecordType.INTENT_REVISION)
    create_record(repo, "i2", RecordType.INTENT_REVISION)
    create_record(repo, "o1", RecordType.OBSERVATION)

    create_relation(repo, "dep", RelationType.DEPENDS_ON, "i1", "a1")
    create_relation(repo, "inv", RelationType.INVALIDATES, "o1", "a1")
    create_relation(repo, "sup", RelationType.SUPERSEDES, "i2", "i1")

    state = repo.effective_state()
    for record_id in ("a1", "i1", "i2", "o1"):
        effective = state.state(record_id)
        assert effective.validity is ValidityState.NOT_INVALIDATED
        assert effective.revision is RevisionState.CURRENT
        assert effective.freshness is FreshnessState.FRESH


def test_invalidation_propagates_transitively_over_bound_dependencies(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "a1", RecordType.ASSUMPTION)
    create_record(repo, "i1", RecordType.INTENT_REVISION)
    create_record(repo, "task1", RecordType.TASK)
    create_record(repo, "o1", RecordType.OBSERVATION)

    create_relation(repo, "dep_i1_a1", RelationType.DEPENDS_ON, "i1", "a1")
    create_relation(repo, "dep_task_i1", RelationType.DEPENDS_ON, "task1", "i1")
    create_relation(repo, "inv_o1_a1", RelationType.INVALIDATES, "o1", "a1")

    repo.bind_dependency("dep_i1_a1", actor=ACTOR)
    repo.bind_dependency("dep_task_i1", actor=ACTOR)
    repo.invalidate("inv_o1_a1", actor=ACTOR)

    state = repo.effective_state()
    assumption = state.state("a1")
    assert assumption.validity is ValidityState.INVALIDATED
    assert assumption.freshness is FreshnessState.FRESH
    assert assumption.invalidated_by_record_ids == ("o1",)

    intent = state.state("i1")
    assert intent.validity is ValidityState.NOT_INVALIDATED
    assert intent.freshness is FreshnessState.STALE
    assert intent.stale_causes[0].root_record_id == "a1"
    assert intent.stale_causes[0].root_kind is StalenessRootKind.INVALIDATED
    assert [
        hop.relation_id for hop in intent.stale_causes[0].dependency_path
    ] == ["dep_i1_a1"]

    task = state.state("task1")
    assert task.freshness is FreshnessState.STALE
    assert [
        hop.relation_id for hop in task.stale_causes[0].dependency_path
    ] == ["dep_task_i1", "dep_i1_a1"]

    assert [item.record_id for item in state.affected("a1")] == ["i1", "task1"]


def test_supersession_marks_old_revision_and_stales_bound_dependents(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "i1", RecordType.INTENT_REVISION)
    create_record(repo, "i2", RecordType.INTENT_REVISION)
    create_record(repo, "task1", RecordType.TASK)

    create_relation(repo, "dep_task_i1", RelationType.DEPENDS_ON, "task1", "i1")
    create_relation(repo, "sup_i2_i1", RelationType.SUPERSEDES, "i2", "i1")
    repo.bind_dependency("dep_task_i1", actor=ACTOR)
    repo.supersede("sup_i2_i1", actor=ACTOR)

    state = repo.effective_state()
    old_intent = state.state("i1")
    assert old_intent.revision is RevisionState.SUPERSEDED
    assert old_intent.superseded_by_record_id == "i2"
    assert old_intent.freshness is FreshnessState.FRESH

    new_intent = state.state("i2")
    assert new_intent.revision is RevisionState.CURRENT
    assert new_intent.freshness is FreshnessState.FRESH

    task = state.state("task1")
    assert task.freshness is FreshnessState.STALE
    assert task.stale_causes[0].root_record_id == "i1"
    assert task.stale_causes[0].root_kind is StalenessRootKind.SUPERSEDED


def test_state_activation_requires_matching_relation_type(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "a", RecordType.CLAIM)
    create_record(repo, "b", RecordType.CLAIM)
    create_relation(repo, "rel_support", RelationType.SUPPORTS, "a", "b")

    before = len(repo.event_store.events())
    with pytest.raises(StateProjectionError, match="must have type depends_on"):
        repo.bind_dependency("rel_support", actor=ACTOR)
    assert len(repo.event_store.events()) == before


def test_supersession_requires_same_record_type(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "new", RecordType.INTENT_REVISION)
    create_record(repo, "old", RecordType.ASSUMPTION)
    create_relation(repo, "sup", RelationType.SUPERSEDES, "new", "old")

    with pytest.raises(StateProjectionError, match="same type"):
        repo.supersede("sup", actor=ACTOR)


def test_supersession_lineage_rejects_branching_and_noncurrent_source(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    for record_id in ("i1", "i2", "i3", "i4"):
        create_record(repo, record_id, RecordType.INTENT_REVISION)

    create_relation(repo, "sup_i2_i1", RelationType.SUPERSEDES, "i2", "i1")
    create_relation(repo, "sup_i3_i1", RelationType.SUPERSEDES, "i3", "i1")
    create_relation(repo, "sup_i1_i4", RelationType.SUPERSEDES, "i1", "i4")
    repo.supersede("sup_i2_i1", actor=ACTOR)

    with pytest.raises(StateProjectionError, match="already superseded through relation"):
        repo.supersede("sup_i3_i1", actor=ACTOR)
    with pytest.raises(StateProjectionError, match="was already superseded"):
        repo.supersede("sup_i1_i4", actor=ACTOR)


def test_duplicate_state_activation_is_rejected(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "i1", RecordType.INTENT_REVISION)
    create_record(repo, "a1", RecordType.ASSUMPTION)
    create_relation(repo, "dep", RelationType.DEPENDS_ON, "i1", "a1")
    repo.bind_dependency("dep", actor=ACTOR)

    with pytest.raises(StateProjectionError, match="already has an M0.3 state activation"):
        repo.bind_dependency("dep", actor=ACTOR)


def test_privileged_malformed_state_event_fails_semantic_operations_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "a1", RecordType.ASSUMPTION)
    create_record(repo, "o1", RecordType.OBSERVATION)
    create_relation(repo, "inv", RelationType.INVALIDATES, "o1", "a1")
    relation = repo.graph().relation("inv")

    payload = semantic_relation_event_payload(relation)
    payload["relation_content_hash"] = "sha256:" + "0" * 64
    repo.event_store.append(
        EventDraft(
            stream_id="state:inv",
            event_type=RECORD_INVALIDATED_EVENT_TYPE,
            actor=ACTOR,
            payload=payload,
        )
    )

    with pytest.raises(StateProjectionError, match="does not bind"):
        repo.effective_state()
    with pytest.raises(StateProjectionError, match="does not bind"):
        create_record(repo, "blocked", RecordType.CLAIM)


def test_state_activation_refuses_head_change_after_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "i1", RecordType.INTENT_REVISION)
    create_record(repo, "a1", RecordType.ASSUMPTION)
    create_relation(repo, "dep", RelationType.DEPENDS_ON, "i1", "a1")

    original_append = causal_module.append_if_ledger_head
    injected = False

    def inject_concurrent_event(store, draft, **precondition):
        nonlocal injected
        if not injected:
            injected = True
            SQLiteEventStore(store.path).append(
                EventDraft(
                    stream_id="external",
                    event_type="external.note.v1",
                    actor=ACTOR,
                    payload={"concurrent": True},
                )
            )
        return original_append(store, draft, **precondition)

    monkeypatch.setattr(causal_module, "append_if_ledger_head", inject_concurrent_event)

    with pytest.raises(LedgerReadError, match="ledger head changed"):
        repo.bind_dependency("dep", actor=ACTOR)

    assert repo.effective_state().state("i1").freshness is FreshnessState.FRESH
