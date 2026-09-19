from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import SQLiteEventStore


def _draft(index: int) -> EventDraft:
    return EventDraft(
        stream_id="project:test",
        event_type="RecordCreated",
        actor=Actor(kind=ActorKind.HUMAN, id=f"worker-{index}"),
        payload={"index": index},
    )


def test_concurrent_first_appends_share_one_atomic_initialization(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    workers = 4
    barrier = Barrier(workers)

    def append_from_worker(index: int):
        barrier.wait()
        return SQLiteEventStore(path).append(_draft(index))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        events = list(executor.map(append_from_worker, range(workers)))

    assert sorted(event.sequence for event in events) == [1, 2, 3, 4]

    store = SQLiteEventStore(path)
    verification = store.verify()
    assert verification.valid is True
    assert verification.checked_events == workers
    assert {event.payload["index"] for event in store.events()} == set(range(workers))
