"""Host-restart worker deaths are bounded infrastructure interruptions, not crashes.

Drives the real ``detect_crashed_workers`` / ``release_stale_claims`` /
``enforce_max_runtime`` paths on an isolated HERMES_HOME, so the
``infra-interruptions`` FORK ANCHOR is what is under test. A worker's spawn
fingerprint is ``"<instantiation epoch>|<start ticks>"``; a fingerprint whose
epoch is not the current boot's proves the host (VM / container) restarted
after the spawn, which is what killed the worker.

Live incident this reproduces (hermes-fork board, 2026-09-26 00:39:16, first
dispatcher tick after a VM reboot): six running cards booked
``pid N not alive``; the shared fingerprint made the systemic breaker trip each
at ``failures=1, effective_limit=1`` with a sticky ``gave_up``.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

from hermes_fork.kanban import infra_interruptions as ii

NOW_EPOCH = "boot-now:100"
OLD_EPOCH = "boot-old:100"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Never touch the operator's board: drop every inherited Kanban pin first.
    for key in [k for k in os.environ if k.startswith("HERMES_KANBAN_")]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(ii, "_current_epoch", lambda: NOW_EPOCH)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    db = kb.kanban_db_path().resolve()
    assert db.is_relative_to(tmp_path.resolve()), f"kanban DB escaped the sandbox: {db}"
    with kbc.connect() as conn:
        yield conn
    kbd._recent_worker_exits.clear()


def _running(conn, tid: str, pid: int, *, epoch: str = OLD_EPOCH, claim_ttl: int = 900) -> None:
    """Claim ``tid`` for host-local worker ``pid`` spawned under boot ``epoch``."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    fp = f"{epoch}|777" if epoch is not None else None
    run_id = kb._current_run_id(conn, tid)
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=?, started_at=?, claim_expires=? WHERE id=?",
        (pid, fp, int(time.time()) - 120, int(time.time()) + claim_ttl, tid),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, worker_started_at=?, claim_expires=? WHERE id=?",
        (pid, fp, int(time.time()) + claim_ttl, run_id),
    )
    conn.commit()


def _write_trailer(tid: str, rc: int) -> None:
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"worker output\n{KANBAN_WORKER_EXIT_TRAILER}{rc}\n")


def _kinds(conn, tid: str) -> list[str]:
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def _interrupted(conn, tid: str) -> bool:
    return any(
        kb._json_dict(r["payload"]).get("reason") == ii.REASON
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='reclaimed'", (tid,))
    )


def _last_run(conn, tid: str):
    return conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()


# --- the demonstrated misclassification --------------------------------------


def test_reboot_killed_workers_do_not_trip_the_systemic_breaker(board):
    """Three cards whose workers died with the previous boot: requeued, nothing counted,
    no sticky ``gave_up`` (upstream books ``pid N not alive`` and trips at 1)."""
    tids = [kb.create_task(board, title=f"t{i}", assignee="a") for i in range(3)]
    for i, tid in enumerate(tids):
        _running(board, tid, 81000 + i)

    kbd.detect_crashed_workers(board)
    kb.recompute_ready(board)

    for tid in tids:
        task = kb.get_task(board, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert "gave_up" not in _kinds(board, tid)
        assert "crashed" not in _kinds(board, tid)
        assert _interrupted(board, tid)
        run = _last_run(board, tid)
        assert run["outcome"] == "reclaimed"
        meta = kb._json_dict(run["metadata"])
        assert meta["host_restart_interrupted"] is True
        assert meta["infrastructure"] is True
        assert meta["spawn_epoch"] == OLD_EPOCH
        assert "host restart" in (task.last_failure_error or "")
    assert kbd.detect_crashed_workers._last_auto_blocked == []


def test_host_restart_streak_is_bounded_then_counts(board):
    """Interruptions are neutral only up to the cap; past it each one is counted, so a
    card whose run keeps taking the host down still reaches the breaker."""
    tid = kb.create_task(board, title="reboots-host", assignee="a")
    cap = ii.MAX_HOST_RESTART_INTERRUPTIONS
    for i in range(cap):
        _running(board, tid, 82000 + i)
        kbd.detect_crashed_workers(board)
        assert kb.get_task(board, tid).consecutive_failures == 0
    _running(board, tid, 82100)
    kbd.detect_crashed_workers(board)
    task = kb.get_task(board, tid)
    assert task.consecutive_failures == 1
    assert task.status == "ready"
    assert _last_run(board, tid)["outcome"] == "crashed"
    assert "not alive" in (_last_run(board, tid)["error"] or "")
    _running(board, tid, 82101)
    kbd.detect_crashed_workers(board)
    kb.recompute_ready(board)
    assert kb.get_task(board, tid).status == "blocked"
    assert "gave_up" in _kinds(board, tid)


def test_a_completed_run_resets_the_allowance(board):
    tid = kb.create_task(board, title="t", assignee="a")
    for i in range(ii.MAX_HOST_RESTART_INTERRUPTIONS):
        _running(board, tid, 83000 + i)
        kbd.detect_crashed_workers(board)
    assert ii.host_restart_streak(board, tid) == ii.MAX_HOST_RESTART_INTERRUPTIONS
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(board, tid, claimer=f"{host}:ok")
    assert kb.complete_task(board, tid, summary="done", force=True)
    assert ii.host_restart_streak(board, tid) == 0
    board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    board.commit()
    _running(board, tid, 83100)
    kbd.detect_crashed_workers(board)
    assert kb.get_task(board, tid).consecutive_failures == 0
    assert _last_run(board, tid)["outcome"] == "reclaimed"


def test_allowance_does_not_age_out_behind_many_non_completed_runs(board):
    """Only a completed run resets the allowance: a long tail of closed, non-completed
    runs (e.g. operator requeues of a failing card) must not push the spent
    interruptions out of view and re-grant a neutral reboot death."""
    tid = kb.create_task(board, title="t", assignee="a")
    cap = ii.MAX_HOST_RESTART_INTERRUPTIONS
    for i in range(cap):
        _running(board, tid, 84000 + i)
        kbd.detect_crashed_workers(board)
    now = int(time.time())
    board.executemany(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, error) "
        "VALUES (?, 'crashed', ?, ?, 'crashed', 'rc=1')",
        [(tid, now, now)] * 60,
    )
    board.commit()
    assert ii.host_restart_streak(board, tid) == cap
    _running(board, tid, 84100)
    kbd.detect_crashed_workers(board)
    assert _last_run(board, tid)["outcome"] == "crashed"
    assert kb.get_task(board, tid).consecutive_failures == 1
    neutral = [
        r for r in board.execute("SELECT metadata FROM task_runs WHERE task_id=?", (tid,))
        if kb._json_dict(r["metadata"]).get("host_restart_interrupted")
    ]
    assert len(neutral) == cap


# --- everything else keeps upstream's booking --------------------------------


def test_same_boot_dead_pid_without_witness_is_still_a_counted_crash(board):
    tid = kb.create_task(board, title="t", assignee="a")
    _running(board, tid, 84000, epoch=NOW_EPOCH)
    kbd.detect_crashed_workers(board)
    task = kb.get_task(board, tid)
    assert task.consecutive_failures == 1
    assert not _interrupted(board, tid)


@pytest.mark.parametrize("epoch", [NOW_EPOCH, OLD_EPOCH])
def test_external_kill_with_exit_witness_is_still_a_counted_crash(board, epoch):
    """SIGKILL from outside the dispatcher (operator ``kill -9``, OOM killer) that this
    process reaped: indistinguishable from a task that exhausts memory, so upstream's
    counted crash stands — even when the spawn fingerprint is from another epoch."""
    tid = kb.create_task(board, title="t", assignee="a")
    _running(board, tid, 84100, epoch=epoch)
    kbd._record_worker_exit(84100, signal.SIGKILL)  # raw wait status: killed by signal 9
    kbd.detect_crashed_workers(board)
    assert kb.get_task(board, tid).consecutive_failures == 1
    assert "killed by signal 9" in (_last_run(board, tid)["error"] or "")
    assert not _interrupted(board, tid)


def test_exit_witness_beats_the_epoch(board):
    """A worker that left a real exit status is booked on it even across a reboot: a
    genuine crash (rc=1) counts, a quota exit (75) stays upstream's neutral requeue."""
    crash = kb.create_task(board, title="crash", assignee="a")
    quota = kb.create_task(board, title="quota", assignee="a")
    _running(board, crash, 84200)
    _write_trailer(crash, 1)
    _running(board, quota, 84201)
    _write_trailer(quota, kb.KANBAN_RATE_LIMIT_EXIT_CODE)

    kbd.detect_crashed_workers(board)

    assert kb.get_task(board, crash).consecutive_failures == 1
    assert not _interrupted(board, crash)
    assert kb.get_task(board, quota).consecutive_failures == 0
    assert _last_run(board, quota)["outcome"] == "rate_limited"
    assert not _interrupted(board, quota)


@pytest.mark.parametrize("fingerprint", [None, "unverified", "12345", "|777"])
def test_no_epoch_evidence_fails_safe_to_counted_crash(board, fingerprint):
    """Legacy / unverified / epoch-less fingerprints (macOS, Windows: no ``/proc``)
    prove nothing about a restart, so upstream's booking stands."""
    tid = kb.create_task(board, title="t", assignee="a")
    _running(board, tid, 84300)
    board.execute("UPDATE tasks SET worker_started_at=? WHERE id=?", (fingerprint, tid))
    board.execute("UPDATE task_runs SET worker_started_at=? WHERE task_id=?", (fingerprint, tid))
    board.commit()
    kbd.detect_crashed_workers(board)
    assert kb.get_task(board, tid).consecutive_failures == 1


def test_no_current_epoch_fails_safe(board, monkeypatch):
    monkeypatch.setattr(ii, "_current_epoch", lambda: "")
    tid = kb.create_task(board, title="t", assignee="a")
    _running(board, tid, 84400)
    kbd.detect_crashed_workers(board)
    assert kb.get_task(board, tid).consecutive_failures == 1


def test_dispatcher_owned_max_runtime_kill_is_still_counted(board, monkeypatch):
    """The dispatcher's own timeout kill books ``timed_out`` and counts; the fork seam
    never sees it (the row leaves ``running`` inside ``enforce_max_runtime``)."""
    tid = kb.create_task(board, title="t", assignee="a", max_runtime_seconds=1)
    _running(board, tid, 84500, epoch=NOW_EPOCH)
    board.execute("UPDATE task_runs SET started_at=? WHERE task_id=?", (int(time.time()) - 60, tid))
    board.commit()
    alive = {"v": True}
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: alive["v"])
    monkeypatch.setattr(kbd, "_pid_recycled", lambda _pid, _s: False)

    def _signal(pid, sig):
        alive["v"] = False

    assert kbd.enforce_max_runtime(board, signal_fn=_signal) == [tid]
    kbd.detect_crashed_workers(board)
    assert kb.get_task(board, tid).consecutive_failures == 1
    assert "timed_out" in _kinds(board, tid)
    assert not _interrupted(board, tid)


def test_expired_claim_after_reboot_stays_a_counted_stale_reclaim(board):
    """An outage longer than the claim TTL is booked by the stale-claim reclaim, which
    runs first in the tick and keeps counting: stale expirations need the breaker."""
    tid = kb.create_task(board, title="t", assignee="a")
    _running(board, tid, 84600, claim_ttl=-10)
    assert kb.release_stale_claims(board) == 1
    kbd.detect_crashed_workers(board)
    task = kb.get_task(board, tid)
    assert task.consecutive_failures == 1
    assert "reclaimed" in _kinds(board, tid)
    assert not _interrupted(board, tid)


def test_classifier_is_pure_on_fingerprints():
    assert ii.spawn_epoch("boot-a:1|777") == "boot-a:1"
    assert ii.spawn_epoch("boot-a:1|") == "boot-a:1"
    for fp in (None, "", "unverified", "777", 777, "|777"):
        assert ii.spawn_epoch(fp) is None
    assert ii.host_restarted_since(OLD_EPOCH, NOW_EPOCH) is True
    assert ii.host_restarted_since(NOW_EPOCH, NOW_EPOCH) is False
    assert ii.host_restarted_since(None, NOW_EPOCH) is False
    assert ii.host_restarted_since(OLD_EPOCH, "") is False
