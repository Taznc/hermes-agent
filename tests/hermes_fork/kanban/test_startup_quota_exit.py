"""A provider quota wall at worker startup must requeue the card, not burn its retry budget.

Live incident (hermes-next board, t_60a5bf65 runs 176/177, 2026-10-03): the Anthropic pool
had benched every key for claude-sonnet-5. ``_anthropic_token_or_raise`` raised the
rate-limit-tagged AuthError (b55a022916), ``chat()`` returned None before any turn ran, and
the dispatcher-spawned ``chat -q`` worker (the NON-quiet path) exited 1 because that exit
ignored ``_credentials_rate_limited``. Two ``crashed`` runs tripped ``failure_limit=2`` and
the card was given up while the quota reset ~59 minutes later.

These tests drive the real raise site -> real ``CLIChatTurnMixin.chat`` -> real
``_run_single_query_mode`` exit (incl. the ``[kanban-worker-exit]`` trailer the worker
writes to its log) -> real ``detect_crashed_workers`` on an isolated board.

The fork's ``startup-quota-exit`` shim was retired when upstream 25b6a9f010 brought the
same fix inline; these tests now pin upstream's ``-q`` exit against that incident.
"""

from __future__ import annotations

import contextlib
import io
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import runtime_provider
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin

MODEL = "claude-sonnet-5"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in [k for k in os.environ if k.startswith("HERMES_KANBAN_")]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    assert kb.kanban_db_path().resolve().is_relative_to(tmp_path.resolve())
    with kbc.connect() as conn:
        yield conn
    kbd._recent_worker_exits.clear()


def _claim_running(conn, tid: str, pid: int) -> None:
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=? WHERE id=?", (pid, kb._current_run_id(conn, tid)),
    )
    conn.commit()


def _worker_shell(monkeypatch, *, startup_error: Exception | None):
    """A ``chat -q`` worker whose credential resolution raises *startup_error*."""

    def resolve_runtime_provider(**kw):
        if startup_error is not None:
            raise startup_error
        return {"provider": "custom", "base_url": "http://x/v1", "api_key": "k"}

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve_runtime_provider)
    shell = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
    shell.model = MODEL
    shell.requested_provider = "anthropic"
    shell._explicit_api_key = shell._explicit_base_url = None
    shell._fallback_model = []
    shell.tool_progress_mode = "full"  # non-quiet: what the dispatcher spawns
    shell.session_id = "worker-session"
    shell._last_turn_result = None
    shell._claim_active_session = lambda *a, **k: True
    shell._show_security_advisories = lambda: None
    shell._print_exit_summary = lambda **k: None
    shell.console = SimpleNamespace(print=lambda *a, **k: None)
    shell._secret_capture_callback = lambda *a, **k: None
    shell.chat = CLIChatTurnMixin.chat.__get__(shell)
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda imgs: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: None)
    return shell


def _run_worker(monkeypatch, tid: str, shell) -> int:
    """Run the worker's ``chat -q`` exit path; its stderr goes to the task log like a real spawn."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exc:
        cli._run_single_query_mode(shell, f"work kanban task {tid}", None, False, True)
    log.write_text(err.getvalue(), encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    return exc.value.code


def _model_cooldown_error(monkeypatch) -> Exception:
    """The real AuthError ``_anthropic_token_or_raise`` raises when the pool benched one model."""
    monkeypatch.setattr(
        "agent.anthropic_credentials.resolve_anthropic_token",
        lambda *, model=None: None if model == MODEL else "sk-ant-still-good",
    )
    try:
        runtime_provider._anthropic_token_or_raise(model=MODEL)
    except Exception as exc:  # noqa: BLE001 - the raise site's own exception is the fixture
        return exc
    raise AssertionError("expected the model-scoped cooldown to raise")


def test_startup_quota_wall_requeues_without_counting_a_failure(board, monkeypatch):
    tid = kb.create_task(board, title="quota", assignee="a")
    for attempt, pid in enumerate((91000, 91001, 91002), start=1):
        _claim_running(board, tid, pid)
        shell = _worker_shell(monkeypatch, startup_error=_model_cooldown_error(monkeypatch))
        assert _run_worker(monkeypatch, tid, shell) == kb.KANBAN_RATE_LIMIT_EXIT_CODE

        kbd.detect_crashed_workers(board)
        kb.recompute_ready(board)

        task = kb.get_task(board, tid)
        assert task.status == "ready", f"attempt {attempt}"
        assert task.consecutive_failures == 0, f"attempt {attempt}"
        run = board.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert run["outcome"] == "rate_limited"
        # Deferred, not respawned straight back into the wall.
        assert kbd.check_respawn_guard(board, tid) == "rate_limit_cooldown"
    kinds = [r["kind"] for r in board.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "crashed" not in kinds and "gave_up" not in kinds
    assert kbd.detect_crashed_workers._last_rate_limited == [tid]


def test_exhausted_provider_429_flows_through_worker_exit_to_dispatcher(board, monkeypatch):
    """A fake provider repeatedly returns HTTP 429 after credential setup succeeds."""
    import httpx
    import openai
    from unittest.mock import MagicMock
    from run_agent import AIAgent

    tid = kb.create_task(board, title="429 in turn", assignee="a")
    _claim_running(board, tid, 93000)
    request = httpx.Request("POST", "https://fake.example/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"message": "rate limit exceeded"}})
    calls = []

    def provider_call(_kwargs):
        calls.append(1)
        raise openai.RateLimitError("HTTP 429: rate limit exceeded", response=response,
                                    body={"error": {"message": "rate limit exceeded"}})

    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *a, **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *a, **kw: {})
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda *a, **kw: MagicMock())
    agent = AIAgent(api_key="fake", base_url="https://fake.example/v1", provider="custom",
                    model="fake-model", quiet_mode=True, skip_context_files=True, skip_memory=True,
                    fallback_model=[])
    agent._api_max_retries = 3
    monkeypatch.setattr(agent, "_interruptible_api_call", provider_call)
    monkeypatch.setattr(agent, "_persist_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **kw: None)
    monkeypatch.setattr("agent.agent_runtime_helpers.time.sleep", lambda *_: None)
    shell = _worker_shell(monkeypatch, startup_error=None)

    def chat(query, *, images=None):
        shell._last_turn_result = agent.run_conversation(query)
        return shell._last_turn_result.get("final_response")

    shell.chat = chat
    assert _run_worker(monkeypatch, tid, shell) == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert len(calls) >= 3, "the provider must exhaust its retry budget"
    assert shell._last_turn_result["failure_reason"] == "rate_limit"
    kbd.detect_crashed_workers(board)
    kb.recompute_ready(board)
    assert kb.get_task(board, tid).consecutive_failures == 0
    assert kb.get_task(board, tid).status == "ready"
    assert board.execute("SELECT outcome FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == "rate_limited"
    assert kbd.check_respawn_guard(board, tid) == "rate_limit_cooldown"


def test_a_real_startup_failure_still_counts(board, monkeypatch):
    """Control: an unclassified startup failure (transport error) keeps exit 1 and is a counted crash."""
    tid = kb.create_task(board, title="crash", assignee="a")
    _claim_running(board, tid, 92000)
    shell = _worker_shell(monkeypatch, startup_error=RuntimeError("fixture transport failure"))
    assert _run_worker(monkeypatch, tid, shell) == 1

    kbd.detect_crashed_workers(board)

    task = kb.get_task(board, tid)
    assert task.consecutive_failures == 1
    run = board.execute(
        "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    assert run["outcome"] == "crashed"


def test_quota_flag_is_ignored_outside_a_kanban_worker(monkeypatch):
    """A person's ``chat -q`` keeps exit 1 on the same wall: 75 is a dispatcher contract."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    shell = _worker_shell(monkeypatch, startup_error=_model_cooldown_error(monkeypatch))
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as exc:
        cli._run_single_query_mode(shell, "do the thing", None, False, True)
    assert exc.value.code == 1


@pytest.mark.parametrize(
    ("turn_result", "expected"),
    [({"completed": True}, 0), ({"interrupted": True}, 130), ({"failed": True}, 1)],
)
def test_a_turn_result_wins_over_a_stale_startup_flag(monkeypatch, turn_result, expected):
    """The flag only describes a run that never produced a turn; a real result decides."""
    from hermes_cli.cli_single_query import _single_query_exit_code

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")
    assert _single_query_exit_code(turn_result, credentials_rate_limited=True) == expected
