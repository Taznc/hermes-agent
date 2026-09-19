"""The dispatcher's non-quiet worker path must reach the existing exit protocol."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert kb.kanban_db_path().resolve().is_relative_to(tmp_path.resolve())


def run_worker(monkeypatch, result):
    """Keep real turn settlement and process boundary; replace only UI/startup."""
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda imgs: [])
    finalized = []
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: finalized.append(c))
    stub = SimpleNamespace(
        _claim_active_session=lambda *a, **k: True,
        console=SimpleNamespace(print=lambda *a, **k: None),
        _show_security_advisories=lambda: None,
        _print_exit_summary=lambda **k: None,
        _prompt_start_time=None,
        _flush_stream=lambda: None,
        conversation_history=[],
        agent=SimpleNamespace(provider="anthropic", session_id="fixture-session"),
        session_id="fixture-session",
    )

    def chat(*a, **k):
        turn = SimpleNamespace(result=result, use_streaming_tts=False, text_queue=None)
        CLIChatTurnMixin._chat_settle_turn(stub, turn)
        return result.get("final_response", "")

    stub.chat = chat
    code = 0
    try:
        cli._run_single_query_mode(stub, "work kanban task", None, False, True)
    except SystemExit as exc:
        code = exc.code
    assert finalized == [stub], "exit must still finalize the CLI"
    return code


@pytest.mark.parametrize("reason,expected", [
    ("rate_limit", kb.KANBAN_RATE_LIMIT_EXIT_CODE),
    ("billing", kb.KANBAN_RATE_LIMIT_EXIT_CODE),
    ("auth", 1),
    (None, 1),  # Current nonretryable 401 result has no failure_reason.
])
@pytest.mark.parametrize("worker", [True, False])
def test_nonquiet_provider_failure_keeps_its_exit_contract(monkeypatch, capsys, reason, expected, worker):
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_failure")
    result = {"failed": True, "error": "provider rejected request", "failure_reason": reason}
    assert run_worker(monkeypatch, result) == (expected if worker else 0)
    if worker and reason in {"rate_limit", "billing"}:
        assert "retry deadline missing or malformed" in capsys.readouterr().err


@pytest.mark.parametrize("already_completed", [False, True])
def test_nonquiet_text_stop_records_or_preserves_exact_run(monkeypatch, already_completed):
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="nonquiet exit boundary", assignee="fixture")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        run_id = task.current_run_id
        if already_completed:
            kb.complete_task(conn, task_id, summary="verified deliverable")
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        code = run_worker(monkeypatch, {"completed": True, "final_response": "finished"})
        run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert run["ended_at"] is not None, "missing lifecycle receipt before worker exits"
        assert kb.get_task(conn, task_id).current_run_id is None
        if already_completed:
            assert code == 0
            assert run["outcome"] == "completed"
            assert run["summary"] == "verified deliverable"
        else:
            assert code == 1
            assert run["outcome"] == "crashed"
            assert "worker_exit_boundary" in run["metadata"]
