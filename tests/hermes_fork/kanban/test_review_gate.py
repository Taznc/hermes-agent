"""Pre-review receipt gate (``kanban.require_pre_review_gate``) at the
``pre-review-gate`` FORK ANCHOR in ``kanban_db.request_review``.

Driven through the real callers — the registered ``kanban_request_review`` tool
handler, the ``hermes kanban request-review`` CLI handler, and the dashboard's
forced ``review`` status verb — against an isolated board and a real
``config.yaml`` read by the normal config loader.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_fork.kanban import review_gate as gate

_KANBAN_ENV = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
    "HERMES_KANBAN_HOME",
)
VALID = {"pre_review_gate": {"revision": "abc1234", "tests": ["pytest t.py: 3 passed"]}}


@pytest.fixture
def board(monkeypatch, tmp_path):
    """Factory: isolated HERMES_HOME + board with the gate set to ``on``;
    returns ``(kb, kbc, home)``. Asserts the DB lives under tmp_path."""
    for var in _KANBAN_ENV:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # An installed reviewer profile, so reviewer routing can be asserted.
    (home / "profiles" / "verifier").mkdir(parents=True)
    (home / "profiles" / "verifier" / "config.yaml").write_text("{}\n")

    def make(*, on: object = True):
        value = str(on).lower() if isinstance(on, bool) else on
        (home / "config.yaml").write_text(
            f"kanban:\n  require_pre_review_gate: {value}\n", encoding="utf-8")
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "NOT ISOLATED"
        return kb, kbc, home

    return make


def _claimed_worker(kb, kbc, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="gated", assignee="test-worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    return tid, claimed.current_run_id


def _tool_request_review(**args):
    import tools.kanban_tools  # noqa: F401  (registers the tool)
    from tools.registry import registry
    return json.loads(registry.get_entry("kanban_request_review").handler(
        {"summary": "implemented the thing", **args}))


def _snapshot(kb, kbc, tid):
    with kbc.connect_closing() as conn:
        t = kb.get_task(conn, tid)
        return (t.status, t.assignee, t.current_run_id), kb.list_events(conn, tid)


# --- tool path -------------------------------------------------------------


@pytest.mark.parametrize("metadata, missing", [
    (None, ["revision", "tests"]),
    ({"notes": "no gate here"}, ["revision", "tests"]),
    ({"pre_review_gate": {"revision": "abc1234", "tests": ""}}, ["tests"]),
    ({"pre_review_gate": {"revision": "  ", "tests": ["t.py: 3 passed"]}}, ["revision"]),
    ({"pre_review_gate": {"revision": "abc1234", "tests": [" ", ""]}}, ["tests"]),
    ({"pre_review_gate": "ran the tests"}, ["revision", "tests"]),
])
def test_tool_refuses_handoff_without_receipt_and_leaves_task_untouched(
        board, monkeypatch, metadata, missing):
    kb, kbc, _ = board(on=True)
    tid, run_id = _claimed_worker(kb, kbc, monkeypatch)
    before = _snapshot(kb, kbc, tid)

    out = _tool_request_review(**({} if metadata is None else {"metadata": metadata}),
                               reviewer="verifier")

    assert out.get("ok") is not True
    err = out["error"]
    assert "review handoff refused" in err
    for key in missing:
        assert f"pre_review_gate.{key}" in err
    for key in {"revision", "tests"} - set(missing):
        assert f"pre_review_gate.{key}" not in err
    assert _snapshot(kb, kbc, tid) == before
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
        assert kb.latest_run(conn, tid).id == run_id


def test_tool_valid_receipt_lands_review_and_routes_to_reviewer(board, monkeypatch):
    kb, kbc, _ = board(on=True)
    tid, _ = _claimed_worker(kb, kbc, monkeypatch)

    out = _tool_request_review(metadata=VALID, reviewer="verifier")

    assert out["ok"] is True, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", "verifier")
        events = [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"]
        assert len(events) == 1
        assert events[0].payload["implementer"] == "test-worker"
        assert events[0].payload["reviewer"] == "verifier"
        run = kb.latest_run(conn, tid)
        assert run.outcome == "review_requested"
        assert run.metadata["pre_review_gate"]["revision"] == "abc1234"


def test_gate_off_keeps_upstream_behaviour(board, monkeypatch):
    kb, kbc, _ = board(on=False)
    tid, _ = _claimed_worker(kb, kbc, monkeypatch)

    out = _tool_request_review()

    assert out["ok"] is True, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"


# --- CLI and dashboard paths -------------------------------------------------


def _cli_request_review(tid, metadata=None, force=False):
    from hermes_cli import kanban as kcli
    args = argparse.Namespace(
        task_id=tid, summary="done", metadata=None if metadata is None else json.dumps(metadata),
        reviewer="verifier", force=force)
    return kcli._cmd_request_review(args)


def test_cli_refuses_without_receipt_then_accepts_valid(board, capsys):
    kb, kbc, _ = board(on=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="cli", assignee="test-worker")
    before = _snapshot(kb, kbc, tid)

    assert _cli_request_review(tid) != 0
    assert "pre_review_gate.revision" in capsys.readouterr().err
    assert _snapshot(kb, kbc, tid) == before

    assert _cli_request_review(tid, metadata=VALID) == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", "verifier")


def test_force_override_bypasses_gate(board):
    """Dashboard drag-to-review and CLI --force are explicit human overrides."""
    kb, kbc, _ = board(on=True)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="dash", assignee="test-worker")
        assert kb.request_review(conn, tid, summary="x", reviewer="verifier") is False
        ok, reason = kb.request_review(conn, tid, summary="x", reviewer="verifier",
                                       with_reason=True)
        assert ok is False and "pre_review_gate" in reason
        assert kb.request_review(conn, tid, summary="x", reviewer="verifier", force=True) is True
        assert kb.get_task(conn, tid).status == "review"


# --- policy unit -------------------------------------------------------------


@pytest.mark.parametrize("value, enabled", [
    (True, True), (False, False), ('"true"', True), ('"off"', False), ("null", False)])
def test_config_value_parsing(board, value, enabled):
    board(on=value)
    assert gate.gate_enabled() is enabled
