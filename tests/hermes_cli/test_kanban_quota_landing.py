"""SQLite/dispatcher integration contracts for quota recovery on current dev."""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert kb.kanban_db_path().resolve().is_relative_to(tmp_path.resolve())
    kb.init_db()
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr("tools.process_registry._stop_systemd_unit", lambda _unit: True)
    return tmp_path


def running(conn, name, pid, provider=None):
    kwargs = {"model_override": "test-model", "provider_override": provider} if provider else {}
    tid = kb.create_task(conn, title=name, assignee="probe", max_runtime_seconds=1, **kwargs)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    run_id = claimed.current_run_id
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time()) - 30, run_id))
    return tid, run_id


def log_for_current_run(conn, tid, text):
    task = kb.get_task(conn, tid)
    assert task.current_run_id is not None
    with kbd._open_worker_log(task, None) as log:
        log.write(text.encode())


def quota_reap(conn, tid, pid, retry="30"):
    log_for_current_run(conn, tid, f"quota exhausted (429); retry after {retry}s.\n")
    kbd._record_worker_exit(pid, 1 << 8)
    return kbd.detect_crashed_workers(conn)


def test_timeout_ownership_is_committed_before_service_stop(isolated_board, monkeypatch):
    """A restart at the service-stop seam must not excuse a recovered quota run."""
    with kbc.connect() as conn:
        tid, run_id = running(conn, "timeout after recovered quota", 191001, "openai")
        log_for_current_run(conn, tid, "quota exhausted (429); retry after 30s.\nRecovered; still running.\n")

        def stop_at_restart(unit):
            assert unit == f"hermes-worker-kanban-{tid}-run-{run_id}.service"
            with kbc.connect() as observer:
                assert kb.has_pending_timeout_kill_intent(
                    observer, task_id=tid, run_id=run_id, worker_pid=191001,
                )
            raise RuntimeError("simulated dispatcher exit after durable intent")

        monkeypatch.setattr("tools.process_registry._stop_systemd_unit", stop_at_restart)
        with pytest.raises(RuntimeError, match="simulated dispatcher exit"):
            kbd.enforce_max_runtime(conn, signal_fn=lambda *_: pytest.fail("stop must precede raw signal"))
    kb._DISPATCHER_KILL_INTENTS.clear()
    with kbc.connect() as conn:
        kbd._record_worker_exit(191001, int(signal.SIGTERM))
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert kb.active_provider_backoffs(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM kanban_timeout_kill_intents").fetchone()[0] == 0


def test_old_quota_does_not_cross_production_log_run_marker(isolated_board):
    with kbc.connect() as conn:
        tid, first_run = running(conn, "two actual claimed runs", 191002, "auto")
        assert quota_reap(conn, tid, 191002) == []
        assert kb.get_task(conn, tid).status == "ready"
        claimed = kb.claim_task(conn, tid)
        assert claimed.current_run_id != first_run
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (191003, tid))
        log_for_current_run(conn, tid, "TypeError: ordinary crash in the next run\n")
        kbd._record_worker_exit(191003, 1 << 8)
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert tid not in kbd.detect_crashed_workers._last_interrupted


@pytest.mark.linux_only
def test_real_termination_path_escalation_keeps_one_intent(isolated_board, monkeypatch):
    """Drive the production termination helper; only OS delivery is simulated."""
    alive = {191004: True}
    monkeypatch.setattr(kbd, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda _pid: False)
    delivered = []
    with kbc.connect() as conn:
        tid, first_run = running(conn, "escalation then recycled pid", 191004)

        def deliver(pid, sig):
            assert kb.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=first_run, worker_pid=pid)
            assert conn.execute("SELECT COUNT(*) FROM kanban_timeout_kill_intents").fetchone()[0] == 1
            delivered.append(sig)
            if sig == signal.SIGKILL:
                alive[pid] = False

        assert kbd.enforce_max_runtime(conn, signal_fn=deliver) == [tid]
        assert delivered == [signal.SIGTERM, signal.SIGKILL]
        assert conn.execute("SELECT COUNT(*) FROM kanban_timeout_kill_intents").fetchone()[0] == 0
        assert kb.get_task(conn, tid).consecutive_failures == 1
        second = kb.claim_task(conn, tid)
        assert second.current_run_id != first_run
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (191004, tid))
        kbd._record_worker_exit(191004, int(signal.SIGTERM))
        assert kbd.detect_crashed_workers(conn) == []
        assert kbd.detect_crashed_workers._last_interrupted == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_surviving_timeout_worker_retains_claim_and_intent(isolated_board, monkeypatch):
    """A failed termination must not spawn a duplicate or spend failure budget."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda _pid: False)
    with kbc.connect() as conn:
        tid, run_id = running(conn, "timeout worker survived", 191010)
        original = kb.get_task(conn, tid)
        delivered = []
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda _pid, sig: delivered.append(sig)) == []
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.worker_pid == 191010
        assert task.claim_lock == original.claim_lock
        assert task.current_run_id == run_id
        assert task.consecutive_failures == 0
        assert kb.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=run_id, worker_pid=191010)
        assert delivered == [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)]
        assert any(e.kind == "reclaim_deferred" for e in kb.list_events(conn, tid))
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert conn.execute("SELECT COUNT(*) FROM kanban_timeout_kill_intents").fetchone()[0] == 0


def test_dispatch_tick_cleans_old_intents_without_touching_live_identity(isolated_board):
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            for created_at, consumed_at in ((0, None), (0, 1), (int(time.time()), None)):
                conn.execute(
                    "INSERT INTO kanban_timeout_kill_intents(task_id, run_id, worker_pid, signal, created_at, consumed_at) "
                    "VALUES ('probe-intent', 1, 191005, 15, ?, ?)", (created_at, consumed_at),
                )
        kbd.dispatch_once(conn, max_spawn=0, dry_run=True, reconcile_orphans=False)
        rows = conn.execute("SELECT created_at, consumed_at FROM kanban_timeout_kill_intents").fetchall()
        assert len(rows) == 1 and rows[0][0] > 0 and rows[0][1] is None
        assert kb.has_pending_timeout_kill_intent(conn, task_id="probe-intent", run_id=1, worker_pid=191005)
        assert not kb.has_pending_timeout_kill_intent(conn, task_id="probe-intent", run_id=2, worker_pid=191005)


def test_dispatch_tick_resumes_all_same_provider_pauses(isolated_board):
    with kbc.connect() as conn:
        parked = []
        for pid in (191006, 191007):
            tid, _ = running(conn, "same provider paused", pid, "openai")
            assert quota_reap(conn, tid, pid) == []
            parked.append(tid)
        other = kb.create_task(conn, title="unrelated route", assignee="probe", model_override="test-model", provider_override="anthropic")
        auto = kb.create_task(conn, title="explicit auto", assignee="probe", model_override="test-model", provider_override="auto")
        assert all(kb.get_task(conn, tid).status == "scheduled" for tid in parked)
        assert all(kbd.check_respawn_guard(conn, tid) == "provider_backoff" for tid in parked)
        assert kbd.check_respawn_guard(conn, other) != "provider_backoff"
        assert kbd.check_respawn_guard(conn, auto) != "provider_backoff"
    with kbc.connect() as conn:
        assert kb.provider_backoff_until(conn, provider="openai") is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_provider_backoff SET until=0 WHERE provider='openai'")
        kbd.dispatch_once(conn, max_spawn=0, dry_run=True, reconcile_orphans=False)
        assert all(kb.get_task(conn, tid).status == "ready" for tid in parked + [other, auto])
        assert kb.active_provider_backoffs(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM kanban_provider_backoff_tasks").fetchone()[0] == 0
        kbd.dispatch_once(conn, max_spawn=0, dry_run=True, reconcile_orphans=False)
        for tid in parked:
            resumed = [e for e in kb.list_events(conn, tid) if e.kind == "unblocked" and e.payload.get("reason") == "provider_backoff_elapsed"]
            assert len(resumed) == 1


@pytest.mark.parametrize("retry,valid", [("١٢", False), ("1_2", False), ("0", False), ("-1", False), ("+1", False), ("12.0", False), ("oops", False), ("12", True)])
def test_retry_after_validation_reaches_real_reclaim(isolated_board, retry, valid):
    with kbc.connect() as conn:
        tid, _ = running(conn, "live parser", 191008, "openai")
        assert quota_reap(conn, tid, 191008, retry) == []
        assert kb.get_task(conn, tid).status == ("scheduled" if valid else "ready")
        assert (kb.provider_backoff_until(conn, provider="openai") is not None) == valid
        assert kb.read_interruption_streak(conn, task_id=tid) == 1


def test_explicit_auto_quota_never_creates_auto_pause(isolated_board):
    with kbc.connect() as conn:
        tid, _ = running(conn, "explicit auto quota", 191009, "auto")
        assert quota_reap(conn, tid, 191009) == []
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.read_interruption_streak(conn, task_id=tid) == 1
        assert kb.provider_backoff_until(conn, provider="auto") is None
        assert kbd.check_respawn_guard(conn, tid) != "provider_backoff"
