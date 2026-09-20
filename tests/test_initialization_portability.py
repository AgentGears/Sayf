from __future__ import annotations

import os
import sqlite3
from pathlib import Path

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
            WHERE lower(name) NOT GLOB 'sqlite_*'
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
    assert len(os.fsencode(store._initialization_revoked_path.name)) < 255
    assert store._initialization_lock_path.name.startswith(".sayf-init-")
    assert store._initialization_pending_path.name.startswith(".sayf-init-recovery-")
    assert store._initialization_revoked_path.name.startswith(".sayf-init-recovery-")


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
            WHERE lower(name) NOT GLOB 'sqlite_*'
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


def test_verify_rejects_user_table_named_sqlitex(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sqlitex (id INTEGER PRIMARY KEY)")
        connection.commit()

    result = store.verify()

    assert result.valid is False
    assert result.reason == "Sayf ledger schema objects are unexpected or incomplete"


def test_pending_marker_does_not_treat_sqlitex_as_sqlite_owned(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)

    with store._initialization_guard():
        store._ensure_pending_initialization_marker()
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE sqlitex (id INTEGER PRIMARY KEY)")
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
    assert names == {"sqlitex"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX marker publication regression")
def test_pending_marker_publish_failure_leaves_no_final_marker_or_target(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    marker_path = store._initialization_pending_path

    def fail_replace(source, destination):
        raise OSError("forced marker publish failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(
        LedgerReadError,
        match="unable to create initialization pending marker",
    ):
        store.initialize()

    assert not path.exists()
    assert not marker_path.exists()
    assert list(tmp_path.glob(f".{marker_path.name}.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync regression")
def test_pending_marker_directory_sync_brackets_target_initialization(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    states = []

    def record_directory_sync(directory):
        assert directory == tmp_path
        states.append((store._initialization_pending_path.exists(), path.exists()))

    monkeypatch.setattr(
        SQLiteEventStore,
        "_fsync_directory",
        staticmethod(record_directory_sync),
    )

    store.initialize()

    assert states == [(True, False), (False, True)]
    assert store.verify().valid is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync regression")
def test_pending_marker_directory_sync_failure_prevents_target_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)

    def fail_directory_sync(directory):
        assert directory == tmp_path
        assert store._initialization_pending_path.exists()
        assert not path.exists()
        raise OSError("forced directory sync failure")

    monkeypatch.setattr(
        SQLiteEventStore,
        "_fsync_directory",
        staticmethod(fail_directory_sync),
    )

    with pytest.raises(
        LedgerReadError,
        match="unable to create initialization pending marker",
    ):
        store.initialize()

    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync regression")
def test_retry_revalidates_surviving_marker_durability_before_target_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    states = []

    def transient_directory_sync(directory):
        assert directory == tmp_path
        states.append((store._initialization_pending_path.exists(), path.exists()))
        if len(states) == 1:
            raise OSError("transient directory sync failure")

    monkeypatch.setattr(
        SQLiteEventStore,
        "_fsync_directory",
        staticmethod(transient_directory_sync),
    )

    with pytest.raises(
        LedgerReadError,
        match="unable to create initialization pending marker",
    ):
        store.initialize()

    assert store._initialization_pending_path.exists()
    assert not path.exists()

    store.initialize()

    assert states == [(True, False), (True, False), (False, True)]
    assert store.verify().valid is True
    assert path.exists()
    assert not store._initialization_pending_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink durability regression")
def test_marker_unlink_failure_blocks_successful_initialization(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    store.initialize()

    with store._initialization_guard():
        store._ensure_pending_initialization_marker()

    marker_path = store._initialization_pending_path
    original_unlink = Path.unlink

    def fail_marker_unlink(self, *args, **kwargs):
        if self == marker_path:
            raise OSError("forced marker unlink failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)

    with pytest.raises(
        LedgerReadError,
        match="unable to durably revoke initialization pending marker",
    ):
        store.initialize()

    assert marker_path.exists()
    assert store.verify().valid is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync regression")
def test_revocation_sync_failure_blocks_success_and_retry_syncs_absence(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    states = []

    def fail_first_revocation_sync(directory):
        assert directory == tmp_path
        states.append((store._initialization_pending_path.exists(), path.exists()))
        if len(states) == 2:
            raise OSError("forced revocation directory sync failure")

    monkeypatch.setattr(
        SQLiteEventStore,
        "_fsync_directory",
        staticmethod(fail_first_revocation_sync),
    )

    with pytest.raises(
        LedgerReadError,
        match="unable to durably revoke initialization pending marker",
    ):
        store.initialize()

    assert states == [(True, False), (False, True)]
    assert path.exists()
    assert not store._initialization_pending_path.exists()
    assert store.verify().valid is True

    store.initialize()

    assert states == [(True, False), (False, True), (False, True)]
    assert store.verify().valid is True


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


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through regression")
def test_windows_write_through_move_replaces_destination(tmp_path) -> None:
    source = tmp_path / "source.pending"
    destination = tmp_path / "destination.pending"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")

    SQLiteEventStore._windows_move_file_write_through(
        source,
        destination,
        replace_existing=True,
    )

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "new"


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through regression")
def test_windows_initialization_uses_write_through_marker_transitions(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    pending = store._initialization_pending_path
    revoked = store._initialization_revoked_path
    calls = []
    original_move = SQLiteEventStore._windows_move_file_write_through

    def record_move(source, destination, *, replace_existing):
        calls.append((Path(source), Path(destination), replace_existing))
        original_move(source, destination, replace_existing=replace_existing)

    monkeypatch.setattr(
        SQLiteEventStore,
        "_windows_move_file_write_through",
        staticmethod(record_move),
    )

    store.initialize()

    assert len(calls) == 2
    assert calls[0][1:] == (pending, False)
    assert calls[1] == (pending, revoked, True)
    assert store.verify().valid is True
    assert not pending.exists()
    assert not revoked.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through regression")
def test_windows_publish_failure_prevents_target_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)

    def fail_move(source, destination, *, replace_existing):
        raise OSError("forced write-through publication failure")

    monkeypatch.setattr(
        SQLiteEventStore,
        "_windows_move_file_write_through",
        staticmethod(fail_move),
    )

    with pytest.raises(
        LedgerReadError,
        match="unable to create initialization pending marker",
    ):
        store.initialize()

    assert not path.exists()
    assert not store._initialization_pending_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through regression")
def test_windows_recovery_reasserts_existing_marker_write_through(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    pending = store._initialization_pending_path
    revoked = store._initialization_revoked_path

    with store._initialization_guard():
        store._ensure_pending_initialization_marker()
        with sqlite3.connect(path) as connection:
            connection.execute("VACUUM")

    calls = []
    original_move = SQLiteEventStore._windows_move_file_write_through

    def record_move(source, destination, *, replace_existing):
        calls.append((Path(source), Path(destination), replace_existing))
        original_move(source, destination, replace_existing=replace_existing)

    monkeypatch.setattr(
        SQLiteEventStore,
        "_windows_move_file_write_through",
        staticmethod(record_move),
    )

    store.initialize()

    assert len(calls) == 2
    assert calls[0][1:] == (pending, True)
    assert calls[1] == (pending, revoked, True)
    assert store.verify().valid is True


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through regression")
def test_windows_revocation_failure_blocks_success_and_retry(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    pending = store._initialization_pending_path
    revoked = store._initialization_revoked_path
    original_move = SQLiteEventStore._windows_move_file_write_through
    failed = False

    def fail_first_revocation(source, destination, *, replace_existing):
        nonlocal failed
        if Path(source) == pending and Path(destination) == revoked and not failed:
            failed = True
            raise OSError("forced write-through revocation failure")
        original_move(source, destination, replace_existing=replace_existing)

    monkeypatch.setattr(
        SQLiteEventStore,
        "_windows_move_file_write_through",
        staticmethod(fail_first_revocation),
    )

    with pytest.raises(
        LedgerReadError,
        match="unable to durably revoke initialization pending marker",
    ):
        store.initialize()

    assert path.exists()
    assert pending.exists()
    assert store.verify().valid is True

    store.initialize()

    assert store.verify().valid is True
    assert not pending.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through regression")
def test_windows_revoked_tombstone_never_authorizes_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = SQLiteEventStore(path)
    revoked = store._initialization_revoked_path
    original_unlink = Path.unlink

    def keep_revoked_tombstone(self, *args, **kwargs):
        if self == revoked:
            raise OSError("forced tombstone cleanup failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", keep_revoked_tombstone)

    store.initialize()

    assert store.verify().valid is True
    assert not store._initialization_pending_path.exists()
    assert revoked.exists()

    path.unlink()
    with sqlite3.connect(path):
        pass

    with pytest.raises(LedgerReadError, match="schema marker is missing"):
        store.initialize()

    assert not store._initialization_pending_path.exists()
    assert revoked.exists()
