from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.graph import GraphProjectionError
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
)
from sayf.storage import LedgerReadError

ACTOR = Actor(kind=ActorKind.HUMAN, id="tester")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def test_typed_records_and_relations_are_ledger_events(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    assumption = repo.create_record(
        RecordDraft(
            record_id="rec_assumption",
            record_type=RecordType.ASSUMPTION,
            payload={"text": "The API is stable"},
        ),
        actor=ACTOR,
    )
    intent = repo.create_record(
        RecordDraft(
            record_id="rec_intent",
            record_type=RecordType.INTENT_REVISION,
            payload={"goal": "ship"},
        ),
        actor=ACTOR,
    )
    relation = repo.create_relation(
        RelationDraft(
            relation_id="rel_support",
            relation_type=RelationType.SUPPORTS,
            source_id=assumption.id,
            target_id=intent.id,
        ),
        actor=ACTOR,
    )

    events = repo.event_store.events()
    assert [event.event_id for event in events] == [
        "rec_assumption",
        "rec_intent",
        "rel_support",
    ]
    assert assumption.creating_event_id == assumption.id
    assert relation.creating_event_id == relation.id

    mutable = {"items": ["before"]}
    mutable_draft = RecordDraft(
        record_id="rec_detached",
        record_type=RecordType.EVIDENCE,
        payload=mutable,
    )
    detached = repo.create_record(mutable_draft, actor=ACTOR)
    mutable_draft.payload["items"].append("after")
    assert detached.payload == {"items": ["before"]}
    assert repo.graph().record("rec_detached").payload == {"items": ["before"]}

    paths = repo.graph().provenance_paths(assumption.id, intent.id)
    assert len(paths) == 1
    assert [step.relation.id for step in paths[0].steps] == [relation.id]


def test_relation_to_unknown_record_fails_without_appending(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    source = repo.create_record(
        RecordDraft(record_id="rec_source", record_type=RecordType.CLAIM),
        actor=ACTOR,
    )
    before = len(repo.event_store.events())

    with pytest.raises(GraphProjectionError, match="unknown record id rec_missing"):
        repo.create_relation(
            RelationDraft(
                relation_id="rel_bad",
                relation_type=RelationType.SUPPORTS,
                source_id=source.id,
                target_id="rec_missing",
            ),
            actor=ACTOR,
        )

    assert len(repo.event_store.events()) == before


def test_duplicate_record_identity_is_rejected(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    draft = RecordDraft(record_id="rec_same", record_type=RecordType.CLAIM)
    repo.create_record(draft, actor=ACTOR)
    with pytest.raises(LedgerReadError, match="record id already exists"):
        repo.create_record(draft, actor=ACTOR)
    assert len(repo.event_store.events()) == 1


def test_concurrent_same_record_id_has_one_winner(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    draft = RecordDraft(record_id="rec_race", record_type=RecordType.CLAIM)

    def create() -> str:
        try:
            return repo.create_record(draft, actor=ACTOR).id
        except LedgerReadError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: create(), range(2)))

    assert sorted(outcomes) == ["rec_race", "rejected"]
    assert [event.event_id for event in repo.event_store.events()] == ["rec_race"]


def test_malformed_reserved_event_poisoning_fails_semantic_projection_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    repo.event_store.append(
        EventDraft(
            event_id="rec_bad",
            stream_id="record:rec_bad",
            event_type=RECORD_CREATED_EVENT_TYPE,
            actor=ACTOR,
            payload={"unexpected": True},
        )
    )

    with pytest.raises(GraphProjectionError, match="invalid typed event"):
        repo.graph()
    with pytest.raises(GraphProjectionError, match="invalid typed event"):
        repo.create_record(
            RecordDraft(record_id="rec_later", record_type=RecordType.CLAIM),
            actor=ACTOR,
        )
    assert [event.event_id for event in repo.event_store.events()] == ["rec_bad"]


def test_register_and_verify_artifact_record(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    source = tmp_path / "evidence.txt"
    source.write_bytes(b"evidence\n")

    record, descriptor = repo.register_artifact_file(
        source,
        actor=ACTOR,
        media_type="text/plain",
        record_id="rec_artifact",
    )
    assert record.type is RecordType.ARTIFACT
    assert record.payload["digest"] == descriptor.digest
    assert repo.verify_artifact_record(record.id).valid is True

    repo.artifact_store.path_for(descriptor.digest).write_bytes(b"tampered")
    verification = repo.verify_artifact_record(record.id)
    assert verification.valid is False
    assert "digest mismatch" in (verification.reason or "")
