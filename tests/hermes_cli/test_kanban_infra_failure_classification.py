"""Tests for kanban dispatcher infra-death classification.

See docs/kanban/infra-failure-classification.md for the spec this locks
in. Covers:
  1. The pure classifier (classify_infra_exit) in isolation, including the
     explicit SIGTERM/SIGKILL-only signal allowlist.
  2. detect_crashed_workers end-to-end: external-signal deaths, the
     dispatcher's own --max-runtime kill (durable timeout-kill intent,
     including a restart-between-signal-and-reap scenario), startup-window
     dead pids outside the window, and quota-signature detection from the
     worker log (including provider backoff parking + the max-seconds cap).
  3. The persistent per-task interruption streak: increments across infra
     paths, promotion to a counted failure once the cap is exceeded, and
     that the streak survives redispatch but resets on genuine completion
     or an explicit operator unblock.
  4. Config flips: kanban.count_infra_failures=true restores counting,
     kanban.provider_backoff=false disables parking.
  5. Regression guards: iteration-budget exhaustion and ordinary nonzero
     exits are never reclassified as infra; excluded signals (SIGABRT et
     al) always count, even repeatedly, reaching gave_up.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


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


def _make_running_task(conn, *, title: str, pid: int, host=None):
    """Create + claim a task and stamp it ``running`` with the given worker pid."""
    host = host or kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title=title, assignee="a")
    kb.claim_task(conn, tid, claimer=f"{host}:w1")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
        (pid, 0, tid),
    )
    conn.commit()
    return tid


def _write_worker_run_log(task_id: str, run_id: int, text: str) -> None:
    log_path = kb.worker_log_path(task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(kb.worker_log_run_marker(run_id))
        log_f.write(text)


# ---------------------------------------------------------------------------
# 1. Pure classifier — signal allowlist
# ---------------------------------------------------------------------------


def test_classify_infra_exit_sigterm_and_sigkill_are_infra():
    """The explicit allowlist: only SIGTERM and SIGKILL are infra-eligible."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        category, reason = kb.classify_infra_exit(
            exit_kind="signaled", signal_number=int(sig), dispatcher_killed=False,
        )
        assert (category, reason) == ("infra", "external_signal"), sig


@pytest.mark.parametrize("sig", [
    signal.SIGABRT, signal.SIGSEGV, signal.SIGPIPE, signal.SIGBUS, signal.SIGFPE,
    signal.SIGILL, signal.SIGQUIT,
])
def test_classify_infra_exit_excluded_signals_are_always_legit(sig):
    """Every signal off the allowlist is legit regardless of dispatcher_killed
    or startup-window flags — the allowlist is exhaustive, not permissive."""
    for dispatcher_killed in (False, True):
        category, reason = kb.classify_infra_exit(
            exit_kind="signaled", signal_number=int(sig), dispatcher_killed=dispatcher_killed,
        )
        assert category == "legit", (sig, dispatcher_killed)
        assert reason == f"signal_{int(sig)}"


def test_classify_infra_exit_dispatcher_owned_sigterm_is_legit():
    """A SIGTERM/SIGKILL the dispatcher DID send (its own max-runtime kill)
    still counts, even though the signal number is on the allowlist."""
    category, reason = kb.classify_infra_exit(
        exit_kind="signaled", signal_number=int(signal.SIGTERM), dispatcher_killed=True,
    )
    assert (category, reason) == ("legit", "dispatcher_kill")


def test_classify_infra_exit_quota_dict_never_overrides_signal_classification():
    """Quota context may not rewrite a signal's ownership/allowlist result."""
    for exit_kind in ("nonzero_exit", "unknown", "clean_exit"):
        category, reason = kb.classify_infra_exit(
            exit_kind=exit_kind, quota_signal_dict={"retry_after_seconds": 30},
        )
        assert category == "infra", exit_kind
        assert reason == "quota"
    category, reason = kb.classify_infra_exit(
        exit_kind="signaled", signal_number=int(signal.SIGTERM),
        quota_signal_dict={"retry_after_seconds": 30},
    )
    assert (category, reason) == ("infra", "external_signal")


def test_classify_infra_exit_dead_pid_within_startup_window_is_infra():
    category, reason = kb.classify_infra_exit(
        exit_kind="unknown", within_startup_window=True,
    )
    assert (category, reason) == ("infra", "startup_window")


def test_classify_infra_exit_dead_pid_outside_startup_window_is_legit():
    category, reason = kb.classify_infra_exit(
        exit_kind="unknown", within_startup_window=False,
    )
    assert (category, reason) == ("legit", "unknown")


def test_classify_infra_exit_nonzero_exit_never_becomes_infra():
    """Regression guard: a plain nonzero exit can never be infra on its own."""
    category, reason = kb.classify_infra_exit(
        exit_kind="nonzero_exit",
        dispatcher_killed=True,
        within_startup_window=True,
    )
    assert (category, reason) == ("legit", "nonzero_exit")


# ---------------------------------------------------------------------------
# 2. detect_crashed_workers integration
# ---------------------------------------------------------------------------


def test_external_sigkill_death_is_infra_not_crash(kanban_home, monkeypatch):
    """An external SIGKILL the dispatcher never sent classifies as infra:
    consecutive_failures stays 0, task re-queues to ready, an ``interrupted``
    event/outcome (not ``crashed``) is recorded, and it does NOT appear in
    the crashed return value."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        pid = 80001
        tid = _make_running_task(conn, title="external-sigkill", pid=pid)
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGKILL)))

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
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


@pytest.mark.parametrize("sig", [signal.SIGABRT, signal.SIGSEGV, signal.SIGPIPE])
def test_excluded_signal_death_is_a_counted_failure_reaching_gave_up(kanban_home, monkeypatch, sig):
    """Regression guard for the signal-allowlist blocker: SIGABRT/SIGSEGV/
    SIGPIPE deaths are NEVER infra — they must reach ``gave_up`` exactly
    like today after enough repeats, never parking as a neutral interrupt."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title=f"excluded-signal-{int(sig)}", assignee="a")

        # DEFAULT_FAILURE_LIMIT is 2 — two signaled deaths must trip the breaker.
        for i, pid in enumerate((80100 + int(sig), 80200 + int(sig))):
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
            conn.commit()
            kbd._record_worker_exit(pid, _signaled_status(int(sig)))
            crashed = kb.detect_crashed_workers(conn)
            assert tid in crashed
            interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
            assert tid not in interrupted
            task = kb.get_task(conn, tid)
            if i == 0:
                assert task.status == "ready"
            else:
                assert task.status == "blocked"

        events = kb.list_events(conn, tid)
        assert any(e.kind == "gave_up" for e in events)
        assert not any(e.kind == "interrupted" for e in events)


def test_dispatcher_owned_max_runtime_kill_persists_durable_intent_and_still_counts(
    kanban_home, monkeypatch,
):
    """The dispatcher's own --max-runtime kill persists a durable SQLite
    timeout-kill intent BEFORE signalling, and remains a LEGIT counted
    failure — even though SIGTERM is on the infra allowlist."""
    killed = []

    def _signal_fn(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="overrun", assignee="worker", max_runtime_seconds=1,
        )
        kb.claim_task(conn, tid)
        pid = 90001
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        run_id = kb._current_run_id(conn, tid)
        old_started = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old_started, tid))
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (old_started, tid),
            )

        timed_out = kbd.enforce_max_runtime(conn, signal_fn=_signal_fn)
        assert tid in timed_out
        assert killed and killed[0][0] == pid

        # The intent was persisted BEFORE the signal and consumed by this
        # same tick's final accounting — nothing pending afterwards.
        assert kb.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=run_id, worker_pid=pid) is False

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1, (
            "the dispatcher's own max-runtime kill must still count as a legit failure"
        )
        events = kb.list_events(conn, tid)
        assert any(e.kind == "timed_out" for e in events)


def test_timeout_kill_intent_survives_restart_between_signal_and_reap(kanban_home, monkeypatch):
    """Deterministic restart-between-signal-and-reap coverage: a timeout-kill
    intent is persisted, the process is treated as if a DIFFERENT dispatcher
    process reaps it later (in-memory _DISPATCHER_KILL_INTENTS wiped, as a
    real restart would do) via detect_crashed_workers/_classify_dead_worker —
    the durable SQLite intent must still resolve the death as legit."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="restart-window", assignee="worker", max_runtime_seconds=600)
        kb.claim_task(conn, tid)
        pid = 90050
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        run_id = kb._current_run_id(conn, tid)

        # Simulate enforce_max_runtime's persist-before-signal step without
        # actually running the full timeout sweep (isolates the restart case).
        kb.persist_timeout_kill_intent(
            conn, task_id=tid, run_id=run_id, worker_pid=pid, signal=int(signal.SIGTERM),
        )
        assert kb.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=run_id, worker_pid=pid) is True

        # Simulate a dispatcher restart: wipe the in-memory kill-intent dict
        # (this is what actually resets on process restart) while the durable
        # SQLite row survives.
        kbd._kb._DISPATCHER_KILL_INTENTS.clear()
        assert kbd._kb._was_dispatcher_killed(pid) is False

        # A DIFFERENT dispatcher process now reaps the worker as signaled.
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGTERM)))
        crashed = kb.detect_crashed_workers(conn)

        # Despite SIGTERM being on the infra allowlist and the in-memory
        # kill-intent dict being empty, the durable SQLite intent means this
        # is still resolved as a legit, counted failure.
        assert tid in crashed
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)

        # The intent is now consumed.
        assert kb.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=run_id, worker_pid=pid) is False


def test_dead_pid_within_startup_window_is_infra(kanban_home, monkeypatch):
    """A ``pid N not alive`` discovery right after the dispatcher loop marks
    itself started is classified infra (gateway restart signature)."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS", "120")
    kb.mark_dispatcher_process_started()
    try:
        with kb.connect() as conn:
            pid = 80002
            tid = _make_running_task(conn, title="dead-pid-restart", pid=pid)
            # No _record_worker_exit call at all -> "unknown" exit_kind.

            crashed = kb.detect_crashed_workers(conn)
            assert tid not in crashed
            interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
            assert tid in interrupted

            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.consecutive_failures == 0

            events = kb.list_events(conn, tid)
            interrupted_event = next(e for e in events if e.kind == "interrupted")
            assert interrupted_event.payload["reason"] == "startup_window"
    finally:
        os.environ.pop(kb._DISPATCHER_STARTED_AT_ENV, None)


def test_dead_pid_outside_startup_window_is_legit_failure(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    os.environ.pop(kb._DISPATCHER_STARTED_AT_ENV, None)

    with kb.connect() as conn:
        pid = 80003
        tid = _make_running_task(conn, title="dead-pid-crash", pid=pid)

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)


def test_quota_log_signature_detected_from_worker_log_is_infra(kanban_home, monkeypatch):
    """A provider quota/429 signature in the worker's final log lines is
    infra even when the process exited nonzero."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        pid = 80004
        tid = _make_running_task(conn, title="quota-log", pid=pid)
        run_id = kb._current_run_id(conn, tid)
        kbd._record_worker_exit(pid, _exited_status(1))
        _write_worker_run_log(tid, run_id,
            "Codex provider quota exhausted (429); retry after 5841s. "
            "Credentials are still valid.\nGoodbye!\n",
        )

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0

        events = kb.list_events(conn, tid)
        interrupted_event = next(e for e in events if e.kind == "interrupted")
        assert interrupted_event.payload["reason"] == "quota"
        assert interrupted_event.payload["quota_retry_after_seconds"] == 5841


# ---------------------------------------------------------------------------
# 3. Provider backoff: cap, clamp, parsing edge cases
# ---------------------------------------------------------------------------


def test_provider_backoff_parses_only_positive_base10_int():
    assert kb._parse_retry_after("30") == 30
    assert kb._parse_retry_after("0") is None
    assert kb._parse_retry_after("-5") is None
    assert kb._parse_retry_after("abc") is None
    assert kb._parse_retry_after("") is None
    assert kb._parse_retry_after(None) is None
    assert kb._parse_retry_after("3.5") is None
    assert kb._parse_retry_after("0x10") is None


def test_provider_backoff_clamps_to_configured_max(kanban_home):
    with kb.connect() as conn:
        # Huge retry-after is clamped to the cap, with a diagnostic recorded.
        until = kb.register_provider_backoff(
            conn, provider="anthropic", retry_after=999_999, task_id="t_x",
            max_seconds=100,
        )
        assert until is not None
        assert until <= int(time.time()) + 100 + 1

        clamped, diagnostic = kb._clamp_retry_after(999_999, 100)
        assert clamped == 100
        assert diagnostic is not None and "clamped" in diagnostic


@pytest.mark.parametrize("retry_after", [None, 0, -1])
def test_provider_backoff_malformed_or_nonpositive_never_pauses(kanban_home, retry_after):
    with kb.connect() as conn:
        until = kb.register_provider_backoff(
            conn, provider="anthropic", retry_after=retry_after, task_id="t_x",
            max_seconds=86400,
        )
        assert until is None
        assert kb.active_provider_backoffs(conn) == []


def test_provider_backoff_persists_across_reconnect(kanban_home):
    """Durability across dispatcher restarts: a fresh connection still sees
    the pause and check_respawn_guard still defers on it."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="paused-provider", assignee="a",
            model_override="claude-x", provider_override="anthropic",
        )
        kb.register_provider_backoff(
            conn, provider="anthropic", retry_after=60, task_id=tid, max_seconds=86400,
        )

    with kb.connect() as conn2:
        assert kb.provider_backoff_until(conn2, provider="anthropic") is not None
        assert kbd.check_respawn_guard(conn2, tid) == "provider_backoff"


def test_quota_death_with_provider_parks_as_scheduled_and_registers_backoff(
    kanban_home, monkeypatch,
):
    """A quota-signature infra death with a resolvable non-auto provider AND
    a usable retry-after parks the task in ``scheduled`` and registers a
    provider-wide pause protecting sibling tasks on the same provider."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        pid = 80005
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="quota-parked", assignee="a", model_override="claude-x", provider_override="anthropic")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        run_id = kb._current_run_id(conn, tid)
        conn.commit()
        kbd._record_worker_exit(pid, _exited_status(1))
        _write_worker_run_log(tid, run_id, "quota exhausted (429); retry after 120s.\n")

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        assert kb.provider_backoff_until(conn, provider="anthropic") is not None

        # A sibling task pinned to the same provider is guarded too.
        sibling = kb.create_task(conn, title="sibling", assignee="a", model_override="claude-x", provider_override="anthropic")
        assert kbd.check_respawn_guard(conn, sibling) == "provider_backoff"

        # provider: auto tasks remain eligible — they can resolve elsewhere.
        auto_task = kb.create_task(conn, title="auto-task", assignee="a")
        assert kbd.check_respawn_guard(conn, auto_task) != "provider_backoff"


def test_quota_death_without_usable_retry_after_does_not_park_falls_to_interruption_policy(
    kanban_home, monkeypatch,
):
    """Quota-without-usable-backoff is neutral only until the persistent
    interruption cap — it must not create a provider pause and must not
    loop indefinitely (see the interruption-streak tests below)."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        pid = 80006
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="quota-no-retry-after", assignee="a", model_override="claude-x", provider_override="anthropic")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        run_id = kb._current_run_id(conn, tid)
        conn.commit()
        kbd._record_worker_exit(pid, _exited_status(1))
        # Quota signature present but NO parseable "retry after Ns".
        _write_worker_run_log(tid, run_id, "quota exhausted (429).\n")

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        task = kb.get_task(conn, tid)
        # No usable retry-after -> not parked, falls back to ready via the
        # ordinary infra/interruption path.
        assert task.status == "ready"
        assert kb.active_provider_backoffs(conn) == []


# ---------------------------------------------------------------------------
# 4. Persistent interruption streak
# ---------------------------------------------------------------------------


def test_interruption_streak_increments_across_infra_deaths_and_promotes_at_cap(
    kanban_home, monkeypatch,
):
    """max_infra_interruptions default 3: the 4th consecutive infra death
    (streak=4 > cap=3) is routed through normal counted-failure accounting,
    with an operator-visible reason and the streak preserved."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="repeat-interrupt", assignee="a")

        for i in range(3):
            pid = 81000 + i
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
            conn.commit()
            kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGKILL)))
            crashed = kb.detect_crashed_workers(conn)
            assert tid not in crashed
            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.consecutive_failures == 0
            assert kb.read_interruption_streak(conn, task_id=tid) == i + 1

        # 4th consecutive infra death: streak now 4 > default cap 3.
        pid = 81003
        kb.claim_task(conn, tid, claimer=f"{host}:w3")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGKILL)))
        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed, "streak-exceeded infra death must be promoted to a counted crash"
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        # Streak is PRESERVED (not reset) by the promotion itself.
        assert kb.read_interruption_streak(conn, task_id=tid) == 4

        events = kb.list_events(conn, tid)
        gave_up_or_crashed = [e for e in events if e.kind in ("crashed", "gave_up")]
        assert gave_up_or_crashed, "operator-visible event must be recorded on cap exceedance"
        crash_event = next(e for e in events if e.kind in ("crashed", "gave_up"))
        assert "infra_streak" in crash_event.payload
        assert crash_event.payload["infra_streak"] == 4


def test_interruption_streak_never_resets_on_bare_redispatch(kanban_home, monkeypatch):
    """A redispatch (reclaim to ready and re-claim) must NOT reset the
    streak — only a genuine non-interruption terminal outcome or an
    explicit operator reset does."""
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="streak-persists", assignee="a")
        pid = 82000
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGTERM)))
        kb.detect_crashed_workers(conn)
        assert kb.read_interruption_streak(conn, task_id=tid) == 1

        # Redispatch: claim again, this time a plain successful completion —
        # a REDISPATCH by itself (the claim/re-ready cycle) must not have
        # reset anything.
        kb.claim_task(conn, tid, claimer=f"{host}:w2")
        assert kb.read_interruption_streak(conn, task_id=tid) == 1


def test_interruption_streak_resets_on_genuine_completion(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="streak-reset-complete", assignee="a")
        pid = 82001
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGTERM)))
        kb.detect_crashed_workers(conn)
        assert kb.read_interruption_streak(conn, task_id=tid) == 1

        kb.claim_task(conn, tid, claimer=f"{host}:w2")
        assert kb.complete_task(conn, tid, result="done") is True
        assert kb.read_interruption_streak(conn, task_id=tid) == 0


def test_interruption_streak_resets_on_explicit_operator_unblock(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS", "1")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="streak-reset-unblock", assignee="a")
        for i in range(2):
            pid = 82100 + i
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
            conn.commit()
            kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGTERM)))
            kb.detect_crashed_workers(conn)

        # cap=1: the second death (streak=2 > cap 1) promotes to blocked.
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert kb.read_interruption_streak(conn, task_id=tid) == 2

        assert kb.unblock_task(conn, tid) is True
        assert kb.read_interruption_streak(conn, task_id=tid) == 0


def test_max_infra_interruptions_minimum_effective_value_is_one(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS", "0")
    assert kb._resolve_max_infra_interruptions() == 3  # invalid override ignored, default wins
    monkeypatch.setenv("HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS", "-5")
    assert kb._resolve_max_infra_interruptions() == 3
    monkeypatch.setenv("HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS", "1")
    assert kb._resolve_max_infra_interruptions() == 1


# ---------------------------------------------------------------------------
# 5. Config flips
# ---------------------------------------------------------------------------


def test_count_infra_failures_true_restores_pre_classification_behaviour(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_COUNT_INFRA_FAILURES", "true")

    with kb.connect() as conn:
        pid = 80007
        tid = _make_running_task(conn, title="external-sig-counted", pid=pid)
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGKILL)))

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)


def test_provider_backoff_false_avoids_provider_parking(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_PROVIDER_BACKOFF", "false")

    with kb.connect() as conn:
        pid = 80008
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="quota-no-parking", assignee="a", model_override="claude-x", provider_override="anthropic")
        kb.claim_task(conn, tid, claimer=f"{host}:w1")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        run_id = kb._current_run_id(conn, tid)
        conn.commit()
        kbd._record_worker_exit(pid, _exited_status(1))
        _write_worker_run_log(tid, run_id, "quota exhausted (429); retry after 120s.\n")

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        task = kb.get_task(conn, tid)
        assert task.status == "ready"  # not parked, provider_backoff disabled
        assert kb.active_provider_backoffs(conn) == []


# ---------------------------------------------------------------------------
# 6. Regression guards
# ---------------------------------------------------------------------------


def test_iteration_budget_exhausted_still_counts_as_failure(kanban_home):
    """Iteration-budget exhaustion never reaches the infra classifier."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="budget-exhausted", assignee="worker")
        kb.claim_task(conn, tid)

        kbd._record_task_failure(
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
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    os.environ.pop(kb._DISPATCHER_STARTED_AT_ENV, None)

    with kb.connect() as conn:
        pid = 80009
        tid = _make_running_task(conn, title="plain-nonzero", pid=pid)
        kbd._record_worker_exit(pid, _exited_status(1))

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        interrupted = getattr(kb.detect_crashed_workers, "_last_interrupted", [])
        assert tid not in interrupted

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        assert any(e.kind == "crashed" for e in events)
        assert not any(e.kind == "interrupted" for e in events)


def test_dispatcher_timeout_beats_quota_signature_in_its_worker_log(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect() as conn:
        pid = 90100
        tid = _make_running_task(conn, title="timeout after recovered 429", pid=pid)
        run_id = kb._current_run_id(conn, tid)
        _write_worker_run_log(tid, run_id, "quota exhausted (429); retry after 30s.\n")
        kb.persist_timeout_kill_intent(conn, task_id=tid, run_id=run_id, worker_pid=pid, signal=int(signal.SIGTERM))
        kbd._kb._DISPATCHER_KILL_INTENTS.clear()
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGTERM)))
        assert tid in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_prior_run_quota_log_cannot_neutralize_later_ordinary_crash(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect() as conn:
        pid = 90101
        tid = _make_running_task(conn, title="stale quota", pid=pid)
        first_run = kb._current_run_id(conn, tid)
        _write_worker_run_log(tid, first_run, "quota exhausted (429); retry after 30s.\n")
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, worker_pid=NULL, "
            "claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        kb.claim_task(conn, tid)
        pid = 90111
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, _exited_status(1))
        assert tid in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_escalated_timeout_intents_are_fully_consumed_and_cannot_poison_reused_pid(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect() as conn:
        pid = 90102
        tid = _make_running_task(conn, title="escalation", pid=pid)
        first_run = kb._current_run_id(conn, tid)
        for sent_signal in (signal.SIGTERM, signal.SIGKILL):
            kb.persist_timeout_kill_intent(conn, task_id=tid, run_id=first_run, worker_pid=pid, signal=int(sent_signal))
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_timeout_kill_intents WHERE task_id=? AND run_id IS ? AND worker_pid=?",
            (tid, first_run, pid),
        ).fetchone()[0] == 1
        assert kb.consume_timeout_kill_intent(conn, task_id=tid, run_id=first_run, worker_pid=pid)
        assert not kb.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=first_run, worker_pid=pid)

        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, worker_pid=NULL, "
            "claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,),
        )
        conn.commit()
        kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, _signaled_status(int(signal.SIGTERM)))
        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed
        assert tid in getattr(kb.detect_crashed_workers, "_last_interrupted", [])


def test_expired_provider_backoff_resumes_all_parked_tasks_for_provider(kanban_home):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="a", model_override="gpt-5", provider_override="openai")
        second = kb.create_task(conn, title="second", assignee="a", model_override="gpt-5", provider_override="openai")
        other = kb.create_task(conn, title="other", assignee="a", model_override="claude", provider_override="anthropic")
        auto = kb.create_task(conn, title="auto", assignee="a")
        for task_id in (first, second, other, auto):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (task_id,))
        kb.register_provider_backoff(conn, provider="openai", retry_after=30, task_id=first, max_seconds=60)
        kb.register_provider_backoff(conn, provider="openai", retry_after=30, task_id=second, max_seconds=60)
        conn.execute("UPDATE kanban_provider_backoff SET until=0 WHERE provider='openai'")
        conn.commit()
        assert set(kb.release_expired_provider_backoffs(conn)) == {first, second}
        assert kb.get_task(conn, first).status == kb.get_task(conn, second).status == "ready"
        assert kb.get_task(conn, other).status == kb.get_task(conn, auto).status == "scheduled"


@pytest.mark.parametrize("value", ["١٢", "1_2", "0", "-1", "+1", "12.0"])
def test_live_retry_after_parser_rejects_nonpositive_and_non_ascii_values(kanban_home, value):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="parser", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        _write_worker_run_log(tid, run_id, f"quota exhausted (429); retry after {value}s.\n")
        assert kb._detect_quota_exit_signal(tid, run_id=run_id) == {"retry_after_seconds": None}
