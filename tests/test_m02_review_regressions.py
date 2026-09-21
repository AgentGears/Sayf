from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import sayf.artifacts as artifacts_module
import sayf.causal as causal_module
from sayf.artifacts import ArtifactStoreError, ContentAddressedArtifactStore
from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft, LedgerEvent
from sayf.graph import CausalGraph, GraphProjectionError
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RESERVED_CONTRACT_RECORD_TYPES,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
)
from sayf.storage import LedgerReadError, SQLiteEventStore

ACTOR = Actor(kind=ActorKind.HUMAN, id="review-regression")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create_record(
    repo: CausalRepository,
    record_id: str,
    *,
    payload: dict | None = None,
) -> None:
    repo.create_record(
        RecordDraft(
            record_id=record_id,
            record_type=RecordType.CLAIM,
            payload={} if payload is None else payload,
        ),
        actor=ACTOR,
    )


def create_relation(
    repo: CausalRepository,
    relation_id: str,
    source_id: str,
    target_id: str,
    *,
    metadata: dict | None = None,
) -> None:
    repo.create_relation(
        RelationDraft(
            relation_id=relation_id,
            relation_type=RelationType.SUPPORTS,
            source_id=source_id,
            target_id=target_id,
            metadata={} if metadata is None else metadata,
        ),
        actor=ACTOR,
    )


def test_typed_write_refuses_head_change_after_semantic_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = repository(tmp_path)
    create_record(repo, "rec_source")
    create_record(repo, "rec_target")

    original_append = causal_module.append_if_ledger_head
    injected = False

    def inject_concurrent_semantic_poison(store, draft, **precondition):
        nonlocal injected
        if not injected:
            injected = True
            SQLiteEventStore(store.path).append(
                EventDraft(
                    event_id="rec_poison",
                    stream_id="record:rec_poison",
                    event_type=RECORD_CREATED_EVENT_TYPE,
                    actor=ACTOR,
                    payload={"unexpected": True},
                )
            )
        return original_append(store, draft, **precondition)

    monkeypatch.setattr(
        causal_module,
        "append_if_ledger_head",
        inject_concurrent_semantic_poison,
    )

    with pytest.raises(LedgerReadError, match="ledger head changed"):
        create_relation(repo, "rel_must_not_commit", "rec_source", "rec_target")

    event_ids = [event.event_id for event in repo.event_store.events()]
    assert event_ids == ["rec_source", "rec_target", "rec_poison"]
    assert "rel_must_not_commit" not in event_ids

    with pytest.raises(GraphProjectionError, match="invalid typed event"):
        repo.graph()


@pytest.mark.parametrize("record_type", sorted(RESERVED_CONTRACT_RECORD_TYPES, key=str))
def test_contract_bearing_record_types_are_reserved(record_type: RecordType) -> None:
    with pytest.raises(ValidationError, match="reserved until its semantic contract"):
        RecordDraft(record_id="rec_reserved", record_type=record_type)


def test_privileged_malformed_activated_contract_event_poisoning_fails_projection_closed() -> None:
    event = LedgerEvent(
        event_id="rec_gate",
        sequence=1,
        stream_id="record:rec_gate",
        event_type=RECORD_CREATED_EVENT_TYPE,
        occurred_at=datetime(2026, 9, 20, tzinfo=UTC),
        actor=ACTOR,
        payload={
            "record_type": RecordType.GATE_DECISION.value,
            "schema_version": 1,
            "content_hash": "sha256:" + "0" * 64,
            "payload": {"decision": "ALLOW"},
        },
        previous_event_hash=None,
        event_hash="sha256:" + "1" * 64,
    )

    with pytest.raises(GraphProjectionError, match="invalid typed event"):
        CausalGraph.from_events([event])


def test_path_result_bound_fails_instead_of_returning_partial_paths(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    for record_id in ("a", "b", "c", "d"):
        create_record(repo, record_id)

    create_relation(repo, "rel_ab", "a", "b")
    create_relation(repo, "rel_bd", "b", "d")
    create_relation(repo, "rel_ac", "a", "c")
    create_relation(repo, "rel_cd", "c", "d")

    with pytest.raises(GraphProjectionError, match="configured result bound"):
        repo.graph().provenance_paths("a", "d", max_paths=1)


def test_graph_results_cannot_mutate_projection_state(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_record(repo, "a", payload={"nested": {"items": ["original"]}})
    create_record(repo, "b")
    create_relation(
        repo,
        "rel_ab",
        "a",
        "b",
        metadata={"nested": {"items": ["original"]}},
    )
    graph = repo.graph()

    record = graph.record("a")
    record.payload["nested"]["items"].append("mutated")
    assert graph.record("a").payload == {"nested": {"items": ["original"]}}

    listed_record = graph.records[0]
    listed_record.payload["nested"]["items"].append("listed-mutation")
    assert graph.record("a").payload == {"nested": {"items": ["original"]}}

    relation = graph.relation("rel_ab")
    relation.metadata["nested"]["items"].append("mutated")
    assert graph.relation("rel_ab").metadata == {"nested": {"items": ["original"]}}

    neighbor = graph.neighbors("a")[0]
    neighbor.relation.metadata["nested"]["items"].append("neighbor-mutation")
    assert graph.relation("rel_ab").metadata == {"nested": {"items": ["original"]}}


@pytest.mark.skipif(os.name == "nt", reason="POSIX CAS publication regression")
def test_posix_existing_check_reverifies_target_that_appears_after_missing_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    content = b"concurrent-identical-artifact"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    target = store.path_for(digest)
    original_verify = store._verify_path
    injected = False

    def verify_then_publish(path: Path, expected_digest: str):
        nonlocal injected
        result = original_verify(path, expected_digest)
        if not injected and path == target and not result.valid:
            injected = True
            path.write_bytes(content)
        return result

    monkeypatch.setattr(store, "_verify_path", verify_then_publish)

    descriptor = store.put_bytes(content)

    assert descriptor.digest == digest
    assert descriptor.size_bytes == len(content)
    assert target.read_bytes() == content


@pytest.mark.skipif(os.name == "nt", reason="POSIX CAS directory durability regression")
def test_posix_cas_durably_anchors_new_directory_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "new-parent" / "objects"
    store = ContentAddressedArtifactStore(root)
    synced: list[Path] = []

    def record_sync(path: Path) -> None:
        synced.append(Path(path))

    monkeypatch.setattr(
        ContentAddressedArtifactStore,
        "_fsync_directory",
        staticmethod(record_sync),
    )

    descriptor = store.put_bytes(b"durable-artifact")
    target = store.path_for(descriptor.digest)

    assert tmp_path in synced
    assert root.parent in synced
    assert root in synced
    assert root / "sha256" in synced
    assert target.parent in synced


@pytest.mark.skipif(os.name == "nt", reason="POSIX CAS directory durability regression")
def test_posix_cas_anchors_existing_concurrent_directory_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "concurrent-root"
    root.mkdir()
    store = ContentAddressedArtifactStore(root)
    synced: list[Path] = []

    def record_sync(path: Path) -> None:
        synced.append(Path(path))

    monkeypatch.setattr(
        ContentAddressedArtifactStore,
        "_fsync_directory",
        staticmethod(record_sync),
    )

    store._ensure_directory_durable(root / "sha256" / "ab")

    # Syncing only the existing boundary itself persists future child entries but
    # does not anchor the boundary's own name. Its parent must be synced as well.
    assert root in synced
    assert tmp_path in synced


@pytest.mark.skipif(os.name == "nt", reason="POSIX CAS reuse durability regression")
def test_posix_cas_reuse_fsyncs_target_directory_after_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    content = b"concurrent-valid-object"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    target = store.path_for(digest)
    original_link = artifacts_module.os.link
    collision_seen = False
    synced_after_collision: list[Path] = []

    def racing_link(source, destination, *, follow_symlinks=False):
        nonlocal collision_seen
        original_link(source, destination, follow_symlinks=follow_symlinks)
        collision_seen = True
        raise FileExistsError("simulated publisher visible before directory fsync")

    def record_sync(path: Path) -> None:
        if collision_seen:
            synced_after_collision.append(Path(path))

    monkeypatch.setattr(
        ContentAddressedArtifactStore,
        "_fsync_directory",
        staticmethod(record_sync),
    )
    monkeypatch.setattr(artifacts_module.os, "link", racing_link)

    descriptor = store.put_bytes(content)

    assert descriptor.digest == digest
    assert target.parent in synced_after_collision
    assert target.read_bytes() == content


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber CAS publication regression")
def test_posix_cas_race_never_overwrites_object_that_appears_before_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    content = b"expected immutable artifact"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    target = store.path_for(digest)

    def racing_link(source, destination, *, follow_symlinks=False):
        del source, follow_symlinks
        destination = Path(destination)
        destination.write_bytes(b"racing-corrupt-object")
        raise FileExistsError("simulated racing publisher")

    monkeypatch.setattr(artifacts_module.os, "link", racing_link)

    with pytest.raises(ArtifactStoreError, match="refusing to replace invalid existing"):
        store.put_bytes(content)

    assert target.read_bytes() == b"racing-corrupt-object"
