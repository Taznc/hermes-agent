"""Kanban run analytics capture: model/provider/reasoning/session_id stamped

at spawn, token/cost totals copied from the assignee profile's ``state.db``
at finalize, task-creation lineage (``created_by_task``/``created_by_run``),
and server-computed ``review_round`` on rejection events.

See the kanban-analytics-capture card (t_d22b4177) for the full acceptance
criteria; this file covers AC2-AC6. AC1 (schema) lives in test_kanban_db.py,
AC7 (CLI/Desktop display) is exercised at the CLI layer.
"""

from __future__ import annotations

import re
import subprocess
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _spawn_and_capture(monkeypatch, tmp_path, task):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 5551

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    with kbc.connect_closing() as conn:
        current = kb.get_task(conn, task.id)
        assert current is not None
        if current.current_run_id is None:
            current = kb.claim_task(conn, task.id)
        assert current is not None
        task = current
        kbd._prepare_worker_launch(task)
        kbd._stamp_worker_run_launch(conn, task)
    pid = kbd._default_spawn(task, str(workspace))
    assert pid is not None
    with kbc.connect_closing() as conn:
        kbd._set_worker_pid(conn, task.id, pid, worker_unit=task.worker_unit, task=task)
    return captured


# ---------------------------------------------------------------------------
# AC2: model/provider/reasoning_effort/model_source stamped on the run row
# and the ``spawned`` event payload at spawn time.
# ---------------------------------------------------------------------------


def test_spawn_stamps_explicit_model_override_as_card_override(kanban_home, monkeypatch, tmp_path):
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="elias",
            model_override="claude-sonnet-5", provider_override="anthropic",
            reasoning_effort="high", route_source="explicit",
        )
        task = kb.get_task(conn, tid)

    _spawn_and_capture(monkeypatch, tmp_path, task)

    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, kb.get_task(conn, tid).current_run_id)
        events = kb.list_events(conn, tid)

    assert run.model == "claude-sonnet-5"
    assert run.provider == "anthropic"
    assert run.reasoning_effort == "high"
    assert run.model_source == "card_override"

    spawned = next(e for e in events if e.kind == "spawned")
    assert spawned.payload["model"] == "claude-sonnet-5"
    assert spawned.payload["provider"] == "anthropic"
    assert spawned.payload["model_source"] == "card_override"


def test_spawn_routed_override_uses_routing_source(kanban_home, monkeypatch, tmp_path):
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="t",
            assignee="elias",
            model_override="cheap-model",
            provider_override="openrouter",
            reasoning_effort="low",
            route_source="mechanical",
            route_name="mechanical",
        )
        task = kb.get_task(conn, tid)

    _spawn_and_capture(monkeypatch, tmp_path, task)

    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, kb.get_task(conn, tid).current_run_id)

    assert run.model_source == "routing"
    assert run.model == "cheap-model"
    assert run.provider == "openrouter"


def test_spawn_without_override_stamps_profile_default_source(kanban_home, monkeypatch, tmp_path):
    profile_dir = kanban_home / "profiles" / "elias"
    profile_dir.mkdir(parents=True)
    profile_dir.joinpath("config.yaml").write_text(
        "model:\n  default:\n    provider: anthropic\n    model: claude-sonnet-5\n"
        "agent:\n  reasoning_effort: medium\n",
        encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        task = kb.get_task(conn, tid)

    _spawn_and_capture(monkeypatch, tmp_path, task)

    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, kb.get_task(conn, tid).current_run_id)

    assert run.model_source == "profile_default"
    assert run.model == "claude-sonnet-5"
    assert run.provider == "anthropic"
    assert run.reasoning_effort == "medium"


# ---------------------------------------------------------------------------
# AC3: dispatcher-generated HERMES_SESSION_ID passed to the worker env AND
# stored on task_runs.session_id at spawn.
# ---------------------------------------------------------------------------


_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")


def test_spawn_generates_and_pins_session_id(kanban_home, monkeypatch, tmp_path):
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        task = kb.get_task(conn, tid)

    captured = _spawn_and_capture(monkeypatch, tmp_path, task)

    env_session_id = captured["env"].get("HERMES_SESSION_ID")
    assert env_session_id
    assert _SESSION_ID_RE.match(env_session_id), env_session_id

    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, kb.get_task(conn, tid).current_run_id)
    assert run.session_id == env_session_id
    assert "--use-env-session-id" in captured["cmd"]


def test_cli_runtime_state_honors_dispatcher_session_id(monkeypatch):
    from cli import HermesCLI

    expected = "20260101_000000_abcdef"
    monkeypatch.setenv("HERMES_SESSION_ID", expected)
    cli = HermesCLI.__new__(HermesCLI)
    cli._init_session_store = lambda: None
    cli._init_ui_state = lambda: None
    cli._write_terminal_breadcrumb = lambda: None

    HermesCLI._init_runtime_state(cli, None, use_env_session_id=True)

    assert cli.session_id == expected


def test_cli_runtime_state_ignores_inherited_session_id_without_opt_in(monkeypatch):
    from cli import HermesCLI

    inherited = "20260101_000000_badbad"
    monkeypatch.setenv("HERMES_SESSION_ID", inherited)
    cli = HermesCLI.__new__(HermesCLI)
    cli._init_session_store = lambda: None
    cli._init_ui_state = lambda: None
    cli._write_terminal_breadcrumb = lambda: None

    HermesCLI._init_runtime_state(cli, None, use_env_session_id=False)

    assert cli.session_id != inherited
    assert _SESSION_ID_RE.match(cli.session_id)


def test_crashed_run_has_non_null_session_id(kanban_home, monkeypatch, tmp_path):
    """A run finalized as ``crashed`` (never reaching kanban_complete) must
    still carry the session_id stamped at spawn — that's the whole point of
    stamping it at spawn instead of at a worker-owned lifecycle tool call."""
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        task = kb.get_task(conn, tid)

    _spawn_and_capture(monkeypatch, tmp_path, task)

    with kbc.connect_closing() as conn:
        run_id = kb.get_task(conn, tid).current_run_id
        kbd._record_task_failure(
            conn, tid, "boom", outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        run = kb.get_run(conn, run_id)

    assert run.session_id is not None


# ---------------------------------------------------------------------------
# AC4: token/cost totals copied from the assignee profile's state.db onto
# the run at finalize (any terminal outcome).
# ---------------------------------------------------------------------------


def _seed_profile_state_db(profile_home: Path, session_id: str, **cols) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    db_path = profile_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER, "
        "api_call_count INTEGER, tool_call_count INTEGER, estimated_cost_usd REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, input_tokens, output_tokens, cache_read_tokens, "
        "reasoning_tokens, api_call_count, tool_call_count, estimated_cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, cols.get("input_tokens", 0), cols.get("output_tokens", 0),
            cols.get("cache_read_tokens", 0), cols.get("reasoning_tokens", 0),
            cols.get("api_call_count", 0), cols.get("tool_call_count", 0),
            cols.get("estimated_cost_usd", 0.0),
        ),
    )
    conn.commit()
    conn.close()


def test_complete_task_copies_token_totals_from_profile_state_db(kanban_home, monkeypatch):
    profile_dir = kanban_home / "profiles" / "elias"
    profile_dir.mkdir(parents=True)
    session_id = "20260101_000000_abcdef"
    _seed_profile_state_db(
        profile_dir, session_id, input_tokens=1000, output_tokens=500,
        cache_read_tokens=200, reasoning_tokens=50, api_call_count=12,
        tool_call_count=30, estimated_cost_usd=1.23,
    )
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        run_id = kb.claim_task(conn, tid).current_run_id
        conn.execute("UPDATE task_runs SET session_id = ? WHERE id = ?", (session_id, run_id))
        conn.commit()
        kb.complete_task(conn, tid, summary="done")
        run = kb.get_run(conn, run_id)

    assert run.input_tokens == 1000
    assert run.output_tokens == 500
    assert run.cache_read_tokens == 200
    assert run.reasoning_tokens == 50
    assert run.api_calls == 12
    assert run.tool_calls == 30
    assert run.estimated_cost_usd == 1.23


def test_complete_task_missing_state_db_leaves_tokens_null_and_still_succeeds(kanban_home, caplog):
    """No profile state.db at all (or no matching session row) must not raise —
    finalize succeeds and the token columns just stay NULL."""
    caplog.set_level("DEBUG", logger="hermes_cli.kanban_db")
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        run_id = kb.claim_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET session_id = ? WHERE id = ?",
            ("20260101_000000_ffffff", run_id),
        )
        conn.commit()
        ok = kb.complete_task(conn, tid, summary="done")
        run = kb.get_run(conn, run_id)

    assert ok is True
    assert run.input_tokens is None
    assert run.estimated_cost_usd is None
    analytics_logs = [
        record for record in caplog.records
        if record.getMessage().startswith("kanban run analytics unavailable")
    ]
    assert len(analytics_logs) == 1


def test_complete_task_missing_session_row_leaves_tokens_null_and_logs_once(kanban_home, caplog):
    caplog.set_level("DEBUG", logger="hermes_cli.kanban_db")
    profile_home = kanban_home / "profiles" / "elias"
    profile_home.mkdir(parents=True)
    state = sqlite3.connect(profile_home / "state.db")
    state.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            reasoning_tokens INTEGER, api_call_count INTEGER, tool_call_count INTEGER,
            estimated_cost_usd REAL
        )
        """
    )
    state.commit()
    state.close()

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        conn.execute("UPDATE task_runs SET session_id = ? WHERE id = ?", ("missing-session", run_id))
        conn.commit()
        ok = kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
        run = kb.get_run(conn, run_id)

    assert ok is True
    assert run is not None and run.input_tokens is None
    analytics_logs = [
        record for record in caplog.records
        if record.getMessage().startswith("kanban run analytics unavailable")
    ]
    assert len(analytics_logs) == 1


# ---------------------------------------------------------------------------
# AC5: tasks.created_by_task / created_by_run lineage.
# ---------------------------------------------------------------------------


def test_worker_created_child_carries_parent_run(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="elias")
        run_id = kb.claim_task(conn, parent_id).current_run_id

    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    from tools import kanban_tools as kt

    result = kt._handle_create({"title": "child", "assignee": "elias"})
    import json as _json
    child_id = _json.loads(result)["task_id"]

    with kbc.connect_closing() as conn:
        child = kb.get_task(conn, child_id)
    assert child.created_by_task == parent_id
    assert child.created_by_run == run_id


def test_cli_created_task_has_null_lineage(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias")
        task = kb.get_task(conn, tid)
    assert task.created_by_task is None
    assert task.created_by_run is None


# ---------------------------------------------------------------------------
# AC6: server-computed review_round on changes_requested / review_requested.
# ---------------------------------------------------------------------------


def test_third_rejection_carries_review_round_3(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        last_implementation_run_id = None
        last_review_run_id = None
        for review_round in range(1, 4):
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None and claimed.current_run_id is not None
            last_implementation_run_id = claimed.current_run_id
            ok = kb.request_review(
                conn, tid, summary="done", reviewer="reviewer",
                metadata={"impl_note": review_round},
                expected_run_id=claimed.current_run_id,
            )
            assert ok
            reviewer_claim = kb.claim_review_task(conn, tid)
            assert reviewer_claim is not None and reviewer_claim.current_run_id is not None
            last_review_run_id = reviewer_claim.current_run_id
            ok, _ = kb.request_changes(
                conn, tid, reason="nope", metadata={"review_note": review_round},
                expected_run_id=reviewer_claim.current_run_id,
            )
            assert ok

        events = kb.list_events(conn, tid)
        assert last_implementation_run_id is not None and last_review_run_id is not None
        implementation_run = kb.get_run(conn, last_implementation_run_id)
        review_run = kb.get_run(conn, last_review_run_id)
        assert implementation_run is not None and review_run is not None

    changes_events = [e for e in events if e.kind == "changes_requested"]
    review_events = [e for e in events if e.kind == "review_requested"]
    assert [e.payload["review_round"] for e in changes_events] == [1, 2, 3]
    assert [e.payload["review_round"] for e in review_events] == [1, 2, 3]
    assert implementation_run.metadata == {"impl_note": 3}
    assert review_run.metadata == {"review_note": 3}
