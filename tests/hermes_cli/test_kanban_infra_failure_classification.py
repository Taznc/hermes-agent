"""Tests for kanban dispatcher infra-death classification.

See docs/kanban/infra-failure-classification.md for the spec this locks
in. Covers:
  1. The pure classifier (classify_infra_exit) in isolation.
  2. detect_crashed_workers end-to-end: external-signal deaths, the
     dispatcher's own --max-runtime kill, startup-window dead pids outside
     the window, and quota-signature detection from the worker log.
  3. Config flips: kanban.count_infra_failures=true restores counting.
  4. Regression guards: iteration-budget exhaustion and ordinary nonzero
     exits are never reclassified as infra.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def _signaled_status(signum: int) -> int:
    """Raw wait-status for a WIFSIGNALED child killed by ``signum``."""
    return signum


# ---------------------------------------------------------------------------
# 1. Pure classifier
# ---------------------------------------------------------------------------


def test_classify_infra_exit_quota_signal_wins_regardless_of_exit_kind():
    """A quota/429 log signature is infra no matter how the process exited."""
    for exit_kind in ("nonzero_exit", "signaled", "unknown", "clean_exit"):
        category, reason = kb.classify_infra_exit(
            exit_kind=exit_kind, quota_signal=True,
        )
        assert category == "infra", exit_kind
        assert reason == "quota"


def test_classify_infra_exit_external_signal_is_infra():
    """A SIGKILL/SIGTERM the dispatcher did not send is infra."""
    category, reason = kb.classify_infra_exit(
        exit_kind="signaled", dispatcher_killed=False,
    )
    assert (category, reason) == ("infra", "external_signal")


def test_classify_infra_exit_dispatcher_owned_signal_is_legit():
    """A SIGKILL/SIGTERM the dispatcher DID send (its own kill) still counts."""
    category, reason = kb.classify_infra_exit(
        exit_kind="signaled", dispatcher_killed=True,
    )
    assert (category, reason) == ("legit", "dispatcher_kill")


def test_classify_infra_exit_dead_pid_within_startup_window_is_infra():
    """A dead-pid discovery inside the dispatcher's own startup window is infra."""
    category, reason = kb.classify_infra_exit(
        exit_kind="unknown", within_startup_window=True,
    )
    assert (category, reason) == ("infra", "startup_window")


def test_classify_infra_exit_dead_pid_outside_startup_window_is_legit():
    """The same dead-pid discovery outside the window is a legit failure —
    unchanged from today's ``unknown`` -> ``crashed`` behaviour."""
    category, reason = kb.classify_infra_exit(
        exit_kind="unknown", within_startup_window=False,
    )
    assert (category, reason) == ("legit", "unknown")


def test_classify_infra_exit_nonzero_exit_never_becomes_infra():
    """Regression guard: a plain nonzero exit can never be infra on its own,
    even inside the startup window or with a dispatcher-kill flag set —
    those signals only apply to ``signaled``/``unknown``."""
    category, reason = kb.classify_infra_exit(
        exit_kind="nonzero_exit",
        dispatcher_killed=True,
        within_startup_window=True,
        quota_signal=False,
    )
    assert (category, reason) == ("legit", "nonzero_exit")


# ---------------------------------------------------------------------------
# 2. detect_crashed_workers integration
# ---------------------------------------------------------------------------


def test_external_signal_death_is_infra_not_crash(kanban_home, monkeypatch):
    """A SIGKILL/SIGTERM the dispatcher never sent classifies as ``infra``:
    consecutive_failures stays 0, task re-queues to ready, an ``interrupted``
    event (not ``crashed``) is recorded."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="external-sig", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        pid = 80001
        conn.execute(
            "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
            (pid, 0, tid),
        )
        conn.commit()
        # Reaped as SIGKILL'd (signal 9), but the dispatcher never marked
        # a kill-intent for this pid — an external actor did it.
        _kb._record_worker_exit(pid, _signaled_status(9))

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        interrupted = getattr(_kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid in interrupted

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0

        events = kb.list_events(conn, tid)
        assert any(e.kind == "interrupted" for e in events)
        assert not any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "gave_up" for e in events)
        interrupted_event = next(e for e in events if e.kind == "interrupted")
        assert interrupted_event.payload["reason"] == "external_signal"

        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "interrupted" in outcomes
        assert "crashed" not in outcomes


def test_dispatcher_owned_max_runtime_kill_still_counts_as_legit_failure(
    kanban_home, monkeypatch,
):
    """The dispatcher's own --max-runtime kill remains a LEGIT failure: it
    counts against the budget and can still reach ``gave_up`` — even though
    it is a SIGTERM/SIGKILL, exactly the signal kind infra classification
    would otherwise treat as external."""
    import hermes_cli.kanban_db as _kb

    killed = []

    def _signal_fn(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="overrun", assignee="worker", max_runtime_seconds=1,
        )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 90001)
        old_started = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (old_started, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (old_started, tid),
            )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=_signal_fn)
        assert tid in timed_out
        assert killed and killed[0][0] == 90001

        # The kill-intent registry now shows this dispatcher signalled the
        # pid, so a LATER reap-tick classification (e.g. if the process
        # took a moment to actually die and got reaped as "signaled" on a
        # subsequent tick) would still resolve to legit, not infra.
        assert _kb._was_dispatcher_killed(90001) is True
        category, reason = _kb.classify_infra_exit(
            exit_kind="signaled",
            dispatcher_killed=_kb._was_dispatcher_killed(90001),
        )
        assert (category, reason) == ("legit", "dispatcher_kill")

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1, (
            "the dispatcher's own max-runtime kill must still count as a "
            "legit failure"
        )

        events = kb.list_events(conn, tid)
        assert any(e.kind == "timed_out" for e in events)


def test_dead_pid_within_startup_window_is_infra(kanban_home, monkeypatch):
    """A ``pid N not alive`` discovery right after the dispatcher loop
    marks itself started is classified infra (gateway restart signature)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS", "120")
    # Simulate the dispatcher loop having just (re)started.
    _kb.mark_dispatcher_process_started()
    try:
        with kb.connect() as conn:
            host = _kb._claimer_id().split(":", 1)[0]
            tid = kb.create_task(conn, title="dead-pid-restart", assignee="a")
            kb.claim_task(conn, tid, claimer=f"{host}:w1")
            pid = 80002
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            # No _record_worker_exit call at all -> "unknown" exit_kind,
            # exactly the historical "pid N not alive" signature.

            crashed = kb.detect_crashed_workers(conn)
            assert tid not in crashed
            interrupted = getattr(_kb.detect_crashed_workers, "_last_interrupted", [])
            assert tid in interrupted

            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.consecutive_failures == 0

            events = kb.list_events(conn, tid)
            interrupted_event = next(e for e in events if e.kind == "interrupted")
            assert interrupted_event.payload["reason"] == "startup_window"
    finally:
        os.environ.pop(_kb._DISPATCHER_STARTED_AT_ENV, None)


def test_dead_pid_outside_startup_window_is_legit_failure(kanban_home, monkeypatch):
    """The same ``pid N not alive`` discovery well after dispatcher startup
    (or with no dispatcher-loop marker at all — the default for direct
    ``detect_crashed_workers`` calls, matching pre-classification tests)
    counts as a legit failure exactly like today."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # No mark_dispatcher_process_started() call -> _dispatcher_uptime_seconds()
    # returns None -> within_startup_window is always False.
    os.environ.pop(_kb._DISPATCHER_STARTED_AT_ENV, None)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="dead-pid-crash", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        pid = 80003
        conn.execute(
            "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
            (pid, 0, tid),
        )
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        interrupted = getattr(_kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)


def test_quota_log_signature_detected_from_worker_log_is_infra(
    kanban_home, monkeypatch,
):
    """A provider quota/429 signature in the worker's final log lines is
    infra even when the process exited nonzero (didn't hit the dedicated
    EX_TEMPFAIL sentinel path)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="quota-log", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        pid = 80004
        conn.execute(
            "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
            (pid, 0, tid),
        )
        conn.commit()
        _kb._record_worker_exit(pid, _exited_status(1))  # plain nonzero exit

        # Write the worker log with the verbatim quota signature.
        log_dir = _kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = _kb.worker_log_path(tid)
        log_path.write_text(
            "Codex provider quota exhausted (429); retry after 5841s. "
            "Credentials are still valid.\nGoodbye!\n",
            encoding="utf-8",
        )

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        interrupted = getattr(_kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid in interrupted

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0

        events = kb.list_events(conn, tid)
        interrupted_event = next(e for e in events if e.kind == "interrupted")
        assert interrupted_event.payload["reason"] == "quota"
        assert interrupted_event.payload["quota_retry_after_seconds"] == 5841


# ---------------------------------------------------------------------------
# 3. Config flip: kanban.count_infra_failures=true restores counting
# ---------------------------------------------------------------------------


def test_count_infra_failures_true_restores_pre_classification_behaviour(
    kanban_home, monkeypatch,
):
    """With kanban.count_infra_failures=true, an external-signal death is
    accounted exactly like before this feature: counts as a failure,
    recorded as ``crashed`` (not ``interrupted``)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_COUNT_INFRA_FAILURES", "true")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="external-sig-counted", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        pid = 80005
        conn.execute(
            "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
            (pid, 0, tid),
        )
        conn.commit()
        _kb._record_worker_exit(pid, _signaled_status(9))

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        interrupted = getattr(_kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)


# ---------------------------------------------------------------------------
# 4. Regression guards
# ---------------------------------------------------------------------------


def test_iteration_budget_exhausted_still_counts_as_failure(kanban_home):
    """Iteration-budget exhaustion never reaches the infra classifier: the
    worker itself calls _record_task_failure with outcome='timed_out'
    before the process exits, so the task is not even 'running' by the
    time detect_crashed_workers would look at it. This locks in that the
    failure still counts (regression guard for req #4)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="budget-exhausted", assignee="worker")
        kb.claim_task(conn, tid)

        tripped = kb._record_task_failure(
            conn, tid,
            error=(
                "Iteration budget exhausted (500/500) — task could not "
                "complete within the allowed iterations"
            ),
            outcome="timed_out",
            release_claim=True,
            end_run=True,
            event_payload_extra={"budget_used": 500, "budget_max": 500},
        )

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        assert "Iteration budget exhausted" in (task.last_failure_error or "")

        events = kb.list_events(conn, tid)
        assert any(
            e.kind == "gave_up" and e.payload.get("trigger_outcome") == "timed_out"
            for e in events
        ) or tripped is False  # below default failure_limit(2), first hit doesn't trip
        # Either way, the run outcome recorded is timed_out, never interrupted.
        run_outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "timed_out" in run_outcomes
        assert "interrupted" not in run_outcomes


def test_ordinary_nonzero_exit_still_counts_as_failure(kanban_home, monkeypatch):
    """A plain nonzero exit with no quota/signal/startup-window signal is
    unaffected by this feature: still a legit failure, still 'crashed'."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    os.environ.pop(_kb._DISPATCHER_STARTED_AT_ENV, None)

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="plain-nonzero", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        pid = 80006
        conn.execute(
            "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
            (pid, 0, tid),
        )
        conn.commit()
        _kb._record_worker_exit(pid, _exited_status(1))

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        interrupted = getattr(_kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)
