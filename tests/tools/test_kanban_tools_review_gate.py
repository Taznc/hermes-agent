"""Enforced pre-review gate on ``kanban_request_review``.

With ``kanban.require_pre_review_gate: true`` a handoff whose metadata lacks a
usable ``pre_review_gate`` (non-empty ``revision`` + ``tests``) is refused and
the card stays with its implementer; with the flag off the handoff behaves as it
always has. Driven through the registered tool handler against a real board and
a real ``config.yaml`` read by the normal config loader.
"""
from __future__ import annotations

import json

import pytest

_KANBAN_ENV = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
    "HERMES_KANBAN_HOME",
)


@pytest.fixture
def gated_worker(monkeypatch, tmp_path):
    """Factory: a claimed worker card on an isolated board, with
    ``kanban.require_pre_review_gate`` set to ``gate`` in the real config.yaml."""
    for var in _KANBAN_ENV:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    def make(*, gate: bool):
        (home / "config.yaml").write_text(
            f"kanban:\n  require_pre_review_gate: {str(gate).lower()}\n", encoding="utf-8")
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "NOT ISOLATED"
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="gated", assignee="test-worker")
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        return tid

    return make


def _request_review(metadata=None):
    import tools.kanban_tools  # noqa: F401  (registers the tool)
    from tools.registry import registry
    args = {"summary": "implemented the thing"}
    if metadata is not None:
        args["metadata"] = metadata
    return json.loads(registry.get_entry("kanban_request_review").handler(args))


def _status(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).status


@pytest.mark.parametrize("metadata, missing", [
    (None, ["revision", "tests"]),
    ({"notes": "no gate here"}, ["revision", "tests"]),
    ({"pre_review_gate": {"revision": "abc1234", "tests": ""}}, ["tests"]),
    ({"pre_review_gate": {"revision": "  ", "tests": ["t.py: 3 passed"]}}, ["revision"]),
    ({"pre_review_gate": "ran the tests"}, ["revision", "tests"]),
])
def test_gate_on_refuses_handoff_without_usable_pre_review_gate(
    gated_worker, metadata, missing,
):
    tid = gated_worker(gate=True)

    d = _request_review(metadata)

    assert d.get("ok") is not True
    error = d.get("error", "")
    for key in missing:
        assert f"pre_review_gate.{key}" in error, error
    for key in {"revision", "tests"} - set(missing):
        assert f"pre_review_gate.{key}" not in error, error
    assert "fork-dev-workflow" in error and "4b" in error, error
    assert _status(tid) == "running"


@pytest.mark.parametrize("gate, metadata", [
    (True, {"pre_review_gate": {"revision": "abc1234",
                                "tests": ["tests/x.py: 3 passed"], "lint": "clean"}}),
    (True, {"pre_review_gate": {"revision": "patch:/tmp/x.diff", "tests": "x.py 2 passed"}}),
    (False, None),
])
def test_usable_gate_or_gate_off_hands_off_to_review(gated_worker, gate, metadata):
    tid = gated_worker(gate=gate)

    d = _request_review(metadata)

    assert d.get("ok") is True, d
    assert d["status"] == "review"
    assert _status(tid) == "review"
