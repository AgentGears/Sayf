from __future__ import annotations

import os
import sqlite3

import pytest

from sayf.storage import LedgerReadError, SQLiteEventStore


def test_initialize_does_not_require_hard_links(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.sqlite3"

    def hard_links_unavailable(*args, **kwargs):
        raise OSError("hard links are not supported")

    monkeypatch.setattr(os, "link", hard_links_unavailable)

    store = SQLiteEventStore(path)
    store.initialize()

    result = store.verify()
    assert result.valid is True
    assert result.checked_events == 0


def test_initialize_does_not_convert_preexisting_empty_sqlite_database(tmp_path) -> None:
    path = tmp_path / "existing-empty.sqlite3"
    with sqlite3.connect(path):
        pass

    with pytest.raises(LedgerReadError, match="schema marker is missing"):
        SQLiteEventStore(path).initialize()

    with sqlite3.connect(path) as connection:
        user_objects = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'trigger', 'view')
            """
        ).fetchall()
    assert user_objects == []


def test_missing_read_only_operations_do_not_create_coordination_sidecar(tmp_path) -> None:
    path = tmp_path / "missing.sqlite3"
    store = SQLiteEventStore(path)
    lock_path = store._initialization_lock_path

    verification = store.verify()
    assert verification.valid is False
    assert verification.reason == "ledger database does not exist"
    assert not path.exists()
    assert not lock_path.exists()

    with pytest.raises(LedgerReadError, match="ledger database does not exist"):
        store.events()

    assert not path.exists()
    assert not lock_path.exists()
