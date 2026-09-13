"""Tests for the host-level PID sweep (t_cba0d6b6): detects live kanban-worker
processes whose task row is gone or was never theirs, independent of any
single row-deletion path (the durable half of the t_749b0510 orphan fix).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_sweep as sweep


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _claim_with_real_pid(conn, proc: subprocess.Popen, *, host: str, claimer_suffix: str) -> str:
    """Create+claim a task and stamp it with ``proc.pid`` as its live worker."""
    tid = kb.create_task(conn, title="sweep target", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{host}:{claimer_suffix}")
    kbd._set_worker_pid(conn, tid, proc.pid)
    return tid


def test_sweep_finds_pid_orphaned_by_direct_row_deletion(conn, monkeypatch):
    """A task row deleted via raw SQL (simulating any non-delete_task gap:
    DB restore, manual sqlite3 surgery, a future bulk-delete script) must
    still surface its live worker pid as an orphan."""
    host = kb._claimer_id().split(":", 1)[0]
    proc = subprocess.Popen(["sleep", "30"])
    try:
        tid = _claim_with_real_pid(conn, proc, host=host, claimer_suffix="A")

        # Simulate the exact gap this sweep exists for: drop the row directly,
        # bypassing delete_task's live-worker guard entirely.
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        conn.commit()

        # The worker-shaped cmdline check needs a real "work kanban task <id>"
        # argv; a bare `sleep 30` won't match, so drive the matcher directly
        # against a synthesized cmdline for this real, live pid.
        monkeypatch.setattr(
            sweep, "_live_kanban_worker_pids",
            lambda: {proc.pid: f"hermes -p w --cli chat -q work kanban task {tid}"},
        )

        orphans = sweep.sweep_orphaned_worker_pids(record=False)
        assert any(o["pid"] == proc.pid for o in orphans), (
            f"expected pid {proc.pid} to be reported orphaned; got {orphans}"
        )
        found = next(o for o in orphans if o["pid"] == proc.pid)
        assert found["task_id_from_argv"] == tid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_sweep_ignores_pid_with_matching_row(conn, monkeypatch):
    """A live worker pid that IS claimed by a real running row must not be
    reported — the sweep only flags genuinely orphaned processes."""
    host = kb._claimer_id().split(":", 1)[0]
    proc = subprocess.Popen(["sleep", "30"])
    try:
        tid = _claim_with_real_pid(conn, proc, host=host, claimer_suffix="B")

        monkeypatch.setattr(
            sweep, "_live_kanban_worker_pids",
            lambda: {proc.pid: f"hermes -p w --cli chat -q work kanban task {tid}"},
        )

        orphans = sweep.sweep_orphaned_worker_pids(record=False)
        assert not any(o["pid"] == proc.pid for o in orphans), (
            f"pid {proc.pid} has a matching row and must not be reported: {orphans}"
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_sweep_matches_across_boards(conn, monkeypatch):
    """A claim living on a non-default board must still be found — the sweep
    enumerates every board's DB, not just the current one."""
    host = kb._claimer_id().split(":", 1)[0]
    board_slug = "otherboard"
    kb.create_board(board_slug)
    with kbc.connect(board=board_slug) as other_conn:
        proc = subprocess.Popen(["sleep", "30"])
        try:
            tid = kb.create_task(other_conn, title="cross-board", assignee="w")
            kb.claim_task(other_conn, tid, claimer=f"{host}:C")
            kbd._set_worker_pid(other_conn, tid, proc.pid)

            monkeypatch.setattr(
                sweep, "_live_kanban_worker_pids",
                lambda: {proc.pid: f"hermes -p w --cli chat -q work kanban task {tid}"},
            )

            orphans = sweep.sweep_orphaned_worker_pids(record=False)
            assert not any(o["pid"] == proc.pid for o in orphans), (
                f"pid {proc.pid} is claimed on board {board_slug!r} and must not be "
                f"reported: {orphans}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_sweep_ignores_other_host_claim_lock(conn, monkeypatch):
    """A claim_lock stamped by a DIFFERENT host must not suppress an orphan
    report for a pid number that happens to collide on this host — pids are
    only meaningful within their own host's process table."""
    proc = subprocess.Popen(["sleep", "30"])
    try:
        tid = kb.create_task(conn, title="foreign-host", assignee="w")
        # A different host's claim, coincidentally naming this host's live pid.
        kb.claim_task(conn, tid, claimer=f"some-other-host:{proc.pid}")
        kbd._set_worker_pid(conn, tid, proc.pid)

        monkeypatch.setattr(
            sweep, "_live_kanban_worker_pids",
            lambda: {proc.pid: f"hermes -p w --cli chat -q work kanban task {tid}"},
        )

        orphans = sweep.sweep_orphaned_worker_pids(record=False)
        assert any(o["pid"] == proc.pid for o in orphans), (
            "a foreign-host claim_lock must not suppress this host's orphan report"
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_sweep_records_orphans_to_durable_sidecar(conn, monkeypatch, kanban_home):
    """Detected orphans are appended to a durable on-disk log, not just
    returned/printed, so an ops path notices even off the return value."""
    host = kb._claimer_id().split(":", 1)[0]
    proc = subprocess.Popen(["sleep", "30"])
    try:
        tid = _claim_with_real_pid(conn, proc, host=host, claimer_suffix="D")
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        conn.commit()

        monkeypatch.setattr(
            sweep, "_live_kanban_worker_pids",
            lambda: {proc.pid: f"hermes -p w --cli chat -q work kanban task {tid}"},
        )

        orphans = sweep.sweep_orphaned_worker_pids(record=True)
        assert orphans
        log_path = sweep._orphan_log_path()
        assert log_path.exists()
        contents = log_path.read_text(encoding="utf-8")
        assert tid in contents
        assert str(proc.pid) in contents
    finally:
        proc.terminate()
        proc.wait(timeout=5)
