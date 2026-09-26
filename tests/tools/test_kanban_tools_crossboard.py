"""Cross-board reads: ``kanban_list(board="*")`` and board-less ``kanban_show`` lookup.

Contracts pinned here:
  - ``board="*"`` lists every non-archived board, each row tagged with its slug,
    filters applied per board, ``limit`` capping the merged total.
  - one unreadable board is reported in ``board_errors``, never fails the call.
  - ``kanban_show`` without ``board`` finds a task on another board; an explicit
    ``board`` never falls back.
  - ``"*"`` is refused by every mutating tool.
  - a dispatcher-spawned worker gains no cross-board read.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_KANBAN_ENV = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
    "HERMES_KANBAN_HOME", "HERMES_SESSION_ID")


@pytest.fixture
def boards(monkeypatch, tmp_path):
    """Three boards (default, alpha, beta) plus one archived board, all in tmp."""
    for var in _KANBAN_ENV:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    for slug in ("default", "alpha", "beta", "old"):
        assert str(kb.kanban_db_path(board=slug)).startswith(str(tmp_path)), "NOT ISOLATED"
    kb._INITIALIZED_PATHS.clear()

    ids: dict[str, dict[str, str]] = {}
    seeds = {
        "default": [("d-low", "ops", 0), ("d-high", "ops", 5)],
        "alpha": [("a-mid", "ops", 3), ("a-other", "review", 3)],
        "beta": [("b-high", "ops", 5)],
        "old": [("o-archived-board", "ops", 9)],
    }
    for slug, rows in seeds.items():
        if slug != "default":
            kb.create_board(slug)
        conn = kbc.connect(board=slug)
        try:
            ids[slug] = {t: kb.create_task(conn, title=t, assignee=a, priority=p) for t, a, p in rows}
        finally:
            conn.close()
    kb.write_board_metadata("old", archived=True)
    return ids


def _list(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_list(args))


def _show(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_show(args))


def test_list_all_boards_tags_rows_filters_and_orders(boards):
    d = _list({"board": "*", "assignee": "ops"})
    got = [(t["board"], t["title"]) for t in d["tasks"]]
    # priority desc, then board slug, then the board's own order; archived board excluded.
    assert got == [("beta", "b-high"), ("default", "d-high"), ("alpha", "a-mid"), ("default", "d-low")]
    assert d["count"] == 4 and d["truncated"] is False and d["board_errors"] == []
    assert set(d["boards"]) == {"default", "alpha", "beta"}

    capped = _list({"board": "*", "limit": 2})
    assert [t["title"] for t in capped["tasks"]] == ["b-high", "d-high"]
    assert capped["truncated"] is True and capped["next_limit"] == 4


def test_list_all_boards_reports_unreadable_board_and_keeps_the_rest(boards):
    from hermes_cli import kanban_db as kb
    bad = kb.board_dir("beta") / "kanban.db"
    bad.write_bytes(b"this is not a sqlite database at all" * 200)
    for sidecar in ("-wal", "-shm"):
        Path(str(bad) + sidecar).unlink(missing_ok=True)

    d = _list({"board": "*"})
    assert [e["board"] for e in d["board_errors"]] == ["beta"]
    assert {t["board"] for t in d["tasks"]} == {"default", "alpha"}
    # A read never quarantines or rewrites the broken board.
    assert bad.read_bytes().startswith(b"this is not a sqlite")


def test_show_without_board_resolves_task_on_another_board(boards):
    tid = boards["alpha"]["a-mid"]
    d = _show({"task_id": tid})
    assert d["resolved_board"] == "alpha"
    assert d["packet"]["identity"]["task_id"] == tid

    # Task on the active board: unchanged payload, no resolved_board.
    local = _show({"task_id": boards["default"]["d-low"]})
    assert "resolved_board" not in local

    missing = _show({"task_id": "t_nope0000"})
    assert "not found" in missing["error"]

    star = _show({"task_id": tid, "board": "*"})
    assert star["resolved_board"] == "alpha"
    assert star["packet"]["identity"]["task_id"] == tid


def test_show_with_explicit_board_never_falls_back(boards):
    d = _show({"task_id": boards["alpha"]["a-mid"], "board": "beta"})
    assert "not found" in d["error"]


def test_star_board_is_rejected_by_mutating_tools(boards):
    from tools import kanban_tools as kt
    tid = boards["default"]["d-low"]
    calls = {
        "kanban_comment": (kt._handle_comment, {"task_id": tid, "body": "x"}),
        "kanban_create": (kt._handle_create, {"title": "x", "assignee": "ops"}),
        "kanban_link": (kt._handle_link, {"parent_id": tid, "child_id": tid}),
        "kanban_unblock": (kt._handle_unblock, {"task_id": tid}),
        "kanban_block": (kt._handle_block, {"task_id": tid, "reason": "x"}),
        "kanban_complete": (kt._handle_complete, {"task_id": tid, "summary": "x"}),
        "kanban_heartbeat": (kt._handle_heartbeat, {"task_id": tid}),
        "kanban_attachments": (kt._handle_attachments, {"task_id": tid}),
    }
    for name, (handler, args) in calls.items():
        out = json.loads(handler({**args, "board": "*"}))
        assert "read-only" in out.get("error", ""), (name, out)
    # Nothing was created anywhere.
    assert _list({"board": "*", "limit": 200})["count"] == 5


def test_worker_gains_no_cross_board_reads(boards, monkeypatch):
    own = boards["default"]["d-low"]
    other = boards["alpha"]["a-mid"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)

    assert "not found" in _show({"task_id": other})["error"]
    assert "orchestrator-only" in _show({"task_id": other, "board": "*"})["error"]
    assert "orchestrator-only" in _list({"board": "*"})["error"]
    assert _show({})["packet"]["identity"]["task_id"] == own
