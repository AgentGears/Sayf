from __future__ import annotations

import json
import sqlite3

import pytest

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import LedgerReadError, SQLiteEventStore


def _draft() -> EventDraft:
    return EventDraft(
        stream_id="project:test",
        event_type="RecordCreated",
        actor=Actor(kind=ActorKind.HUMAN, id="tester"),
        payload={"a": 1},
    )


def _rewrite_with_update_guard_restored(path, sql: str, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute(sql, (value,))
        connection.executescript(
            """
            CREATE TRIGGER events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'Sayf ledger events are immutable');
            END;
            """
        )
        connection.commit()


def test_verify_rejects_noncanonical_payload_json_with_same_parsed_value(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft())

    _rewrite_with_update_guard_restored(
        path,
        "UPDATE events SET payload_json = ? WHERE sequence = 1",
        '{"a":0,"a":1}',
    )

    result = store.verify()
    assert result.valid is False
    assert result.failure_sequence == 1
    assert result.reason == "stored payload JSON is not canonical"


def test_verify_rejects_noncanonical_actor_json_with_same_parsed_value(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft())

    with sqlite3.connect(path) as connection:
        raw_actor = connection.execute(
            "SELECT actor_json FROM events WHERE sequence = 1"
        ).fetchone()[0]
    actor = json.loads(raw_actor)
    noncanonical_actor = json.dumps(actor, sort_keys=False, indent=2)
    assert noncanonical_actor != raw_actor

    _rewrite_with_update_guard_restored(
        path,
        "UPDATE events SET actor_json = ? WHERE sequence = 1",
        noncanonical_actor,
    )

    result = store.verify()
    assert result.valid is False
    assert result.failure_sequence == 1
    assert result.reason == "stored actor JSON is not canonical"


def test_verify_rejects_equivalent_noncanonical_timestamp(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft())

    with sqlite3.connect(path) as connection:
        raw_timestamp = connection.execute(
            "SELECT occurred_at FROM events WHERE sequence = 1"
        ).fetchone()[0]
    assert raw_timestamp.endswith("+00:00")
    equivalent_timestamp = raw_timestamp.removesuffix("+00:00") + "Z"

    _rewrite_with_update_guard_restored(
        path,
        "UPDATE events SET occurred_at = ? WHERE sequence = 1",
        equivalent_timestamp,
    )

    result = store.verify()
    assert result.valid is False
    assert result.failure_sequence == 1
    assert result.reason == "stored event timestamp is not canonical"


def test_extra_ledger_metadata_fails_closed_for_verify_init_and_append(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sayf_ledger_meta (key, value) VALUES ('future_flag', 'enabled')"
        )
        connection.commit()

    result = store.verify()
    assert result.valid is False
    assert result.reason == "Sayf ledger metadata is unsupported or malformed"

    with pytest.raises(LedgerReadError, match="metadata is unsupported or malformed"):
        store.initialize()
    with pytest.raises(LedgerReadError, match="metadata is unsupported or malformed"):
        store.append(_draft())


def test_initialize_refuses_hash_invalid_existing_history(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.append(_draft())

    _rewrite_with_update_guard_restored(
        path,
        "UPDATE events SET payload_json = ? WHERE sequence = 1",
        '{"a":2}',
    )

    with pytest.raises(LedgerReadError, match="ledger verification failed at sequence 1"):
        store.initialize()
