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
    pending_path = store._initialization_pending_path

    verification = store.verify()
    assert verification.valid is False
    assert verification.reason == "ledger database does not exist"
    assert not path.exists()
    assert not lock_path.exists()
    assert not pending_path.exists()

    with pytest.raises(LedgerReadError, match="ledger database does not exist"):
        store.events()

    assert not path.exists()
    assert not lock_path.exists()
    assert not pending_path.exists()


def test_coordination_sidecar_name_is_bounded_for_long_ledger_filename(tmp_path) -> None:
    path = tmp_path / ("l" * 240)
    store = SQLiteEventStore(path)

    assert len(os.fsencode(path.name)) == 240
    assert len(os.fsencode(store._initialization_lock_path.name)) < 255
    assert len(os.fsencode(store._initialization_pending_path.name)) < 255
    assert store._initialization_lock_path.name.startswith(".sayf-init-")
    assert store._initialization_pending_path.name.startswith(".sayf-init-recovery-")


@pytest.mark.skipif(os.name == "nt", reason="POSIX component-limit regression")
def test_long_ledger_filename_initializes_without_overlong_sidecar(tmp_path) -> None:
    path = tmp_path / ("l" * 240)
    store = SQLiteEventStore(path)

    store.initialize()

    result = store.verify()
    assert result.valid is True
    assert result.checked_events == 0


def test_case_variants_share_initialization_coordination_key(tmp_path) -> None:
    upper = SQLiteEventStore(tmp_path / "Ledger.sqlite3")
    lower = SQLiteEventStore(tmp_path / "ledger.sqlite3")

    assert upper._initialization_coordination_key == lower._initialization_coordination_key
    assert upper._initialization_lock_path == lower._initialization_lock_path


@pytest.mark.skipif(os.name == "nt", reason="Windows path identity is case-insensitive")
def test_case_variants_use_distinct_recovery_markers_on_case_sensitive_posix(tmp_path) -> None:
    upper = SQLiteEventStore(tmp_path / "Ledger.sqlite3")
    lower = SQLiteEventStore(tmp_path / "ledger.sqlite3")

    assert upper._initialization_lock_path == lower._initialization_lock_path
    assert upper._initialization_recovery_key != lower._initialization_recovery_key
    assert upper._initialization_pending_path != lower._initialization_pending_path


@pytest.mark.skipif(os.name == "nt", reason="Windows path identity is case-insensitive")
def test_pending_marker_cannot_cross_authorize_case_variant_target(tmp_path) -> None:
    upper = SQLiteEventStore(tmp_path / "Ledger.sqlite3")
    lower_path = tmp_path / "ledger.sqlite3"
    lower = SQLiteEventStore(lower_path)

    with upper._initialization_guard():
        upper._ensure_pending_initialization_marker()

    with sqlite3.connect(lower_path):
        pass

    assert upper._initialization_pending_path.exists()
    assert not lower._initialization_pending_path.exists()

    with pytest.raises(LedgerReadError, match="schema marker is missing"):
        lower.initialize()

    with sqlite3.connect(lower_path) as connection:
        user_objects = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'trigger', 'view')
            """
        ).fetchall()
    assert user_objects == []


def test_interrupted_schema_less_initialization_is_recoverable(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    with store._initialization_guard():
        store._ensure_pending_initialization_marker()
        with sqlite3.connect(path) as connection:
            connection.execute("VACUUM")

    assert path.exists()
    assert path.stat().st_size > 0
    assert store._initialization_pending_path.exists()

    store.initialize()

    result = store.verify()
    assert result.valid is True
    assert result.checked_events == 0
    assert not store._initialization_pending_path.exists()


def test_pending_marker_does_not_legitimize_unknown_schema(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    with store._initialization_guard():
        store._ensure_pending_initialization_marker()
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            connection.commit()

    with pytest.raises(
        LedgerReadError,
        match="interrupted initialization target contains user schema objects",
    ):
        store.initialize()

    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert names == {"unrelated"}


def test_stale_pending_marker_is_cleared_after_valid_commit(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with store._initialization_guard():
        store._ensure_pending_initialization_marker()

    assert store._initialization_pending_path.exists()

    store.initialize()

    assert store.verify().valid is True
    assert not store._initialization_pending_path.exists()
