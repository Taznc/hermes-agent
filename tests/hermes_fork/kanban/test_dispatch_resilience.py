"""Fork-owned tests for ``hermes_fork.kanban.dispatch_resilience``.

Extraction target: infra-failure classification, durable timeout-kill-intent
tracking, per-task interruption-streak accounting, and provider-quota
backoff, moved out of ``hermes_cli.kanban_db`` /
``hermes_cli.kanban_db_dispatch`` behind the
``# >>> FORK ANCHOR: kanban-dispatch-resilience <<<`` markers. See
``tests/hermes_cli/test_kanban_infra_failure_classification.py`` for the
end-to-end dispatcher-integration coverage of this same logic reached through
the ``kanban_db``/``kanban_db_dispatch`` facade; these tests instead pin the
extracted module's own contracts: the pure classifiers in isolation, and that
the late-bound ``_kb``/``_kd`` origin references the extraction depends on to
avoid an import cycle actually resolve to the real modules.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import dispatch_resilience as dr


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Re-export identity: the facade attribute IS the extracted function, not a
# copy — proves the anchor import wires the fork module in rather than
# duplicating behavior that could drift.
# ---------------------------------------------------------------------------


def test_kanban_db_reexports_the_extracted_classifier():
    assert kb.classify_infra_exit is dr.classify_infra_exit
    assert kb.register_provider_backoff is dr.register_provider_backoff
    assert kb._DISPATCHER_KILL_INTENTS is dr._DISPATCHER_KILL_INTENTS


def test_kanban_db_dispatch_reexports_the_extracted_crash_reclaim():
    assert kbd.detect_crashed_workers is dr.detect_crashed_workers
    assert kbd._reclaim_dead_workers is dr._reclaim_dead_workers


def test_extraction_late_bound_origins_resolve_to_the_real_modules():
    """The cycle-breaking ``_kb``/``_kd`` module refs must point at the real,
    fully-initialized origin modules, not stand-ins or partial imports."""
    assert dr._kb is kb
    assert dr._kd is kbd


# ---------------------------------------------------------------------------
# Pure classifiers
# ---------------------------------------------------------------------------


def test_classify_infra_exit_signal_allowlist_vs_everything_else():
    """Only SIGTERM/SIGKILL are infra-eligible signals; any other signal is
    always legit, and a dispatcher-owned kill is always legit regardless of
    the signal allowlist."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        category, _ = dr.classify_infra_exit(
            exit_kind="signaled", signal_number=int(sig), dispatcher_killed=False,
        )
        assert category == "infra"

    category, _ = dr.classify_infra_exit(
        exit_kind="signaled", signal_number=int(signal.SIGABRT), dispatcher_killed=False,
    )
    assert category == "legit"

    category, reason = dr.classify_infra_exit(
        exit_kind="signaled", signal_number=int(signal.SIGTERM), dispatcher_killed=True,
    )
    assert (category, reason) == ("legit", "dispatcher_kill")


def test_classify_infra_exit_quota_signal_never_overrides_a_signaled_exit():
    """A quota signature can promote a plain nonzero exit to infra, but must
    never override an already-classified signaled exit."""
    category, _ = dr.classify_infra_exit(
        exit_kind="signaled", signal_number=int(signal.SIGABRT), quota_signal=True,
    )
    assert category == "legit"

    category, reason = dr.classify_infra_exit(exit_kind="nonzero_exit", quota_signal=True)
    assert (category, reason) == ("infra", "quota")


def test_classify_infra_exit_unknown_only_infra_within_startup_window():
    within, reason_within = dr.classify_infra_exit(exit_kind="unknown", within_startup_window=True)
    outside, reason_outside = dr.classify_infra_exit(exit_kind="unknown", within_startup_window=False)
    assert (within, reason_within) == ("infra", "startup_window")
    assert (outside, reason_outside) == ("legit", "unknown")


def test_is_infra_signal_allowlist_is_exactly_sigterm_and_sigkill():
    assert dr._is_infra_signal(int(signal.SIGTERM))
    assert dr._is_infra_signal(int(signal.SIGKILL))
    assert not dr._is_infra_signal(int(signal.SIGABRT))
    assert not dr._is_infra_signal(int(signal.SIGSEGV))


@pytest.mark.parametrize("raw", [None, "", "0", "-5", "abc", "12.5", "1e3"])
def test_parse_retry_after_rejects_non_positive_integers(raw):
    assert dr._parse_retry_after(raw) is None


def test_parse_retry_after_accepts_a_positive_base_ten_integer():
    assert dr._parse_retry_after("42") == 42


def test_clamp_retry_after_none_or_nonpositive_yields_no_pause():
    assert dr._clamp_retry_after(None, 100) == (None, None)
    assert dr._clamp_retry_after(0, 100) == (None, None)
    assert dr._clamp_retry_after(-5, 100) == (None, None)


def test_clamp_retry_after_caps_at_the_configured_max_with_a_diagnostic():
    clamped, diagnostic = dr._clamp_retry_after(500, 100)
    assert clamped == 100
    assert diagnostic is not None and "clamped" in diagnostic


def test_clamp_retry_after_passes_through_a_value_under_the_cap():
    assert dr._clamp_retry_after(30, 100) == (30, None)


# ---------------------------------------------------------------------------
# DB-backed persistence round trips
# ---------------------------------------------------------------------------


def test_interruption_streak_round_trip(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        assert dr.read_interruption_streak(conn, task_id=tid) == 0

        assert dr.increment_interruption_streak(conn, task_id=tid) == 1
        assert dr.increment_interruption_streak(conn, task_id=tid) == 2
        assert dr.read_interruption_streak(conn, task_id=tid) == 2

        dr.reset_interruption_streak(conn, task_id=tid)
        assert dr.read_interruption_streak(conn, task_id=tid) == 0

        dr.increment_interruption_streak(conn, task_id=tid)
        dr.delete_interruption_streak(conn, task_id=tid)
        assert dr.read_interruption_streak(conn, task_id=tid) == 0


def test_timeout_kill_intent_persist_then_consume(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        assert not dr.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=1, worker_pid=999)

        dr.persist_timeout_kill_intent(conn, task_id=tid, run_id=1, worker_pid=999, signal=15)
        assert dr.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=1, worker_pid=999)
        # A different run/pid identity is unaffected.
        assert not dr.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=2, worker_pid=999)

        assert dr.consume_timeout_kill_intent(conn, task_id=tid, run_id=1, worker_pid=999)
        assert not dr.has_pending_timeout_kill_intent(conn, task_id=tid, run_id=1, worker_pid=999)
        # Consuming again finds nothing left to delete.
        assert not dr.consume_timeout_kill_intent(conn, task_id=tid, run_id=1, worker_pid=999)


def test_provider_backoff_register_query_and_release(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")

        # A non-positive retry-after registers no pause at all.
        assert dr.register_provider_backoff(
            conn, provider="acme", retry_after=None, task_id=tid, max_seconds=3600,
        ) is None
        assert dr.provider_backoff_until(conn, provider="acme") is None

        until = dr.register_provider_backoff(
            conn, provider="acme", retry_after=30, task_id=tid, max_seconds=3600,
        )
        assert until is not None
        assert dr.provider_backoff_until(conn, provider="acme") == until
        active = dr.active_provider_backoffs(conn)
        assert any(row["provider"] == "acme" for row in active)

        # A registered backoff parks its task in ``scheduled`` (the caller's
        # job, not register_provider_backoff's) until the pause elapses.
        conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
        # Force the pause to have already elapsed, then release it.
        conn.execute("UPDATE kanban_provider_backoff SET until = 0 WHERE provider = 'acme'")
        conn.commit()
        resumed = dr.release_expired_provider_backoffs(conn)
        assert resumed == [tid]
        assert dr.provider_backoff_until(conn, provider="acme") is None
        assert dr.active_provider_backoffs(conn) == []
