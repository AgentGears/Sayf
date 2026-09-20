from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Barrier, Event

import pytest

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


def test_contender_cannot_classify_in_progress_target_as_preexisting(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    owner = SQLiteEventStore(path)
    contender_started = Event()

    def append_from_contender():
        contender_started.set()
        return SQLiteEventStore(path).append(_draft(99))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with owner._initialization_guard():
            path.touch(exist_ok=False)
            future = executor.submit(append_from_contender)
            assert contender_started.wait(timeout=1.0)

            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.1)

            path.unlink()

        event = future.result(timeout=5.0)

    assert event.sequence == 1
    assert event.payload == {"index": 99}
    assert SQLiteEventStore(path).verify().valid is True
