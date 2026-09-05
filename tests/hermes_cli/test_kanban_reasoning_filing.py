"""Per-task reasoning effort — CLI filing surface (Phase 2.14).

Covers the two filing-surface gaps this card closes: ``hermes kanban
create --reasoning``, the after-the-fact ``hermes kanban set-model
--reasoning`` setter, and the ``kanban_create`` MCP tool schema/handler.
The DB layer (``normalize_reasoning_effort``, ``create_task(reasoning_effort=)``,
``set_reasoning_effort``) and the dispatcher's ``--reasoning`` emission are
already covered elsewhere and are exercised here only incidentally (end to
end, via ``_default_spawn``).

Driven through ``kanban.run_slash`` — the same entry point both the
interactive CLI and the gateway use — rather than hand-building an
``argparse.Namespace``, so these tests exercise the real flag parsing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# `hermes kanban create --reasoning`
# ---------------------------------------------------------------------------


def test_create_help_shows_reasoning_flag(kanban_home):
    out = kc.run_slash("create --help")
    assert "--reasoning" in out


def test_create_with_reasoning_stores_and_dispatches(kanban_home):
    out = kc.run_slash('create "task a" --assignee worker --reasoning high')
    assert "kanban: reasoning_effort must be one of" not in out
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn, assignee="worker")
    assert len(tasks) == 1
    assert tasks[0].reasoning_effort == "high"


def test_create_with_reasoning_none_stores_literal_none(kanban_home):
    kc.run_slash('create "task a" --assignee worker --reasoning none')
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn, assignee="worker")
    assert tasks[0].reasoning_effort == "none"


def test_create_without_reasoning_stores_null(kanban_home):
    kc.run_slash('create "task a" --assignee worker')
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn, assignee="worker")
    assert tasks[0].reasoning_effort is None


def test_create_with_invalid_reasoning_fails_loudly(kanban_home):
    out = kc.run_slash('create "task a" --assignee worker --reasoning bogus-level')
    # Must name the valid set, not silently fall back to the profile default.
    assert "reasoning_effort must be one of" in out
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn, assignee="worker")
    assert tasks == []


def test_create_with_model_and_reasoning_together(kanban_home):
    kc.run_slash(
        'create "task a" --assignee worker '
        "--model claude-opus-4.6 --provider anthropic --reasoning xhigh"
    )
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn, assignee="worker")
    t = tasks[0]
    assert t.model_override == "claude-opus-4.6"
    assert t.provider_override == "anthropic"
    assert t.reasoning_effort == "xhigh"


# ---------------------------------------------------------------------------
# Dispatcher argv end-to-end: both --model and --reasoning reach the worker
# ---------------------------------------------------------------------------


def _spawn_and_capture(monkeypatch, tmp_path, task):
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    return captured["cmd"]


def test_dispatcher_spawns_with_both_model_and_reasoning(kanban_home, monkeypatch, tmp_path):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="elias",
            model_override="glm-5", provider_override="openrouter",
            reasoning_effort="xhigh",
        )
        task = kb.get_task(conn, tid)
    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)
    i = cmd.index("-m")
    assert cmd[i + 1] == "glm-5"
    j = cmd.index("--provider")
    assert cmd[j + 1] == "openrouter"
    k = cmd.index("--reasoning")
    assert cmd[k + 1] == "xhigh"


# ---------------------------------------------------------------------------
# `hermes kanban set-model --reasoning` (after-the-fact setter)
# ---------------------------------------------------------------------------


def test_set_model_reasoning_only_leaves_model_untouched(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker",
            model_override="gpt-5.6-sol", provider_override="openai",
        )
    out = kc.run_slash(f"set-model {tid} --reasoning medium")
    assert "error" not in out.lower()
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.reasoning_effort == "medium"
    # Model override is untouched — a reasoning-only call must not clear it.
    assert t.model_override == "gpt-5.6-sol"
    assert t.provider_override == "openai"


def test_set_model_and_reasoning_together(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
    kc.run_slash(
        f"set-model {tid} claude-opus-4.6 --provider anthropic --reasoning low"
    )
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.model_override == "claude-opus-4.6"
    assert t.provider_override == "anthropic"
    assert t.reasoning_effort == "low"


def test_set_model_reasoning_clear_falls_back_to_profile_default(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker", reasoning_effort="high")
    kc.run_slash(f"set-model {tid} --reasoning clear")
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.reasoning_effort is None


def test_set_model_reasoning_none_is_a_real_value_not_a_clear(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker", reasoning_effort="high")
    kc.run_slash(f"set-model {tid} --reasoning none")
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    # "none" pins thinking OFF — it must be stored literally, not treated
    # as a clear-to-profile-default sentinel.
    assert t.reasoning_effort == "none"


def test_set_model_invalid_reasoning_fails_loudly(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
    out = kc.run_slash(f"set-model {tid} --reasoning bogus-level")
    assert "reasoning_effort must be one of" in out
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.reasoning_effort is None


def test_set_model_with_no_args_at_all_errors(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
    out = kc.run_slash(f"set-model {tid}")
    assert "requires a model" in out or "--reasoning" in out


# ---------------------------------------------------------------------------
# `kanban_create` MCP tool schema + handler
# ---------------------------------------------------------------------------


def test_kanban_create_schema_exposes_reasoning_effort():
    from tools import kanban_tools as kt

    props = kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "reasoning_effort" in props
    assert props["reasoning_effort"]["type"] == "string"


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_handle_create_passes_reasoning_effort_through(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
        "reasoning_effort": "xhigh",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect_closing() as conn:
        child = kb.get_task(conn, d["task_id"])
    assert child.reasoning_effort == "xhigh"


def test_handle_create_rejects_invalid_reasoning_effort(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "reasoning_effort": "bogus-level",
    })
    d = json.loads(out)
    # Tool-handler errors are the registry's tool_error() shape ({"error":
    # ...}), not a success envelope with ok=False — see tools/registry.py.
    assert "error" in d
    assert "reasoning_effort must be one of" in d["error"]


def test_handle_create_omitted_reasoning_effort_stores_null(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_create({"title": "child task", "assignee": "peer"})
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect_closing() as conn:
        child = kb.get_task(conn, d["task_id"])
    assert child.reasoning_effort is None
