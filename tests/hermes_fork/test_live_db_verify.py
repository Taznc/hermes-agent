"""A live connection to state.db must not read as corruption (anchor ``live-db-header-check``).

``verify_sqlite_integrity`` refused its raw header read while this process held a tracked
connection, then reported that refusal as ``cannot read header``. ``hermes update`` printed
"state.db is corrupted" for a healthy database, and with a pre-update snapshot on disk it went on
to restore that snapshot over it. These tests use real SQLite files and real tracked connections.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.backup import verify_sqlite_integrity
from hermes_cli.sqlite_safe_read import connect_tracked, track_connection, untrack_connection
from hermes_cli.update_cmd_maint import _verify_and_restore_one_state_db


def _make_db(path, rows=50):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO sessions (name) VALUES (?)", [(f"s{i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def state_db(tmp_path):
    return _make_db(tmp_path / "state.db")


@pytest.fixture()
def marked_live():
    """Register *path* as live without opening it, for files SQLite could not open anyway."""
    tracked = []

    def _mark(path):
        track_connection(path)
        tracked.append(path)
        return path

    yield _mark
    for path in tracked:
        untrack_connection(path)


def test_healthy_db_with_live_connection_is_valid(state_db):
    conn = connect_tracked(state_db)
    try:
        result = verify_sqlite_integrity(state_db)
    finally:
        conn.close()
    assert result["valid"] is True
    assert result["message"] == "integrity check passed"


def test_live_connection_still_runs_integrity_check_when_pragma_disabled(state_db):
    """Skipping the header must not leave "header check passed" as an unearned verdict."""
    conn = connect_tracked(state_db)
    try:
        result = verify_sqlite_integrity(state_db, run_pragma=False)
    finally:
        conn.close()
    assert result["valid"] is True
    assert result["message"] == "integrity check passed"


def test_zeroed_db_with_live_connection_is_still_invalid(tmp_path, marked_live):
    path = tmp_path / "state.db"
    path.write_bytes(b"\0" * 8192)
    result = verify_sqlite_integrity(marked_live(path))
    assert result["valid"] is False


def test_overwritten_magic_with_live_connection_is_still_invalid(state_db, marked_live):
    raw = bytearray(state_db.read_bytes())
    raw[:16] = b"NOT A SQLITE DB\0"
    state_db.write_bytes(bytes(raw))
    result = verify_sqlite_integrity(marked_live(state_db))
    assert result["valid"] is False


def test_oversized_zeroed_db_with_live_connection_fails_schema_probe(tmp_path, marked_live):
    """Above ``max_bytes`` the schema probe has to catch what the skipped header check would."""
    path = tmp_path / "state.db"
    path.write_bytes(b"\0" * 8192)
    result = verify_sqlite_integrity(marked_live(path), max_bytes=1)
    assert result["valid"] is False


def test_no_live_connection_keeps_the_header_check(tmp_path):
    """Offline verdicts are unchanged: bad magic is named as bad magic, not a pragma failure."""
    path = tmp_path / "state.db"
    path.write_bytes(b"NOT A SQLITE DB\0" + b"\0" * 8192)
    result = verify_sqlite_integrity(path)
    assert result["valid"] is False
    assert result["message"].startswith("missing SQLite header magic")


def test_verifying_with_live_connection_does_not_open_the_file_raw(state_db, monkeypatch):
    """The refusal exists to protect POSIX locks, so the fix must not bypass it with ``force``."""
    from hermes_cli import sqlite_safe_read

    forced = []
    real = sqlite_safe_read.read_header_bytes_preopen

    def _spy(path, *, length=100, force=False):
        forced.append(force)
        return real(path, length=length, force=force)

    monkeypatch.setattr(sqlite_safe_read, "read_header_bytes_preopen", _spy)
    conn = connect_tracked(state_db)
    try:
        verify_sqlite_integrity(state_db)
    finally:
        conn.close()
    assert True not in forced


def test_update_guard_does_not_roll_back_healthy_db_with_live_connection(tmp_path, capsys):
    """The outcome that matters: a stale snapshot must not replace a healthy, in-use database."""
    home = tmp_path / "home"
    home.mkdir()
    state_db = _make_db(home / "state.db", rows=50)
    snapshot_dir = home / "state-snapshots" / "20260701-000000"
    snapshot_dir.mkdir(parents=True)
    _make_db(snapshot_dir / "state.db", rows=1)

    conn = connect_tracked(state_db)
    try:
        _verify_and_restore_one_state_db(home, label="test home")
        rows = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()

    output = capsys.readouterr().out
    assert "corrupted" not in output
    assert "Auto-restored" not in output
    assert rows == 50
