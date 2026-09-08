"""REST surface for the operator dispatch pause (maintenance drain).

The desktop Kanban plugin needs three things to run a safe gateway restart:
see whether the board is paused, pause it, and watch ``running_count`` fall to
zero before restarting. These are behaviour contracts on the endpoints, driven
through the real FastAPI router against a real board DB — same harness shape as
``test_kanban_dashboard_dispatch_caps.py``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_pause_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in os.environ:
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert kb.kanban_db_path().is_relative_to(tmp_path), "NOT ISOLATED"
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    _mod, router = _load_plugin_router()
    app = FastAPI()
    app.include_router(router, prefix=PREFIX)
    return TestClient(app)


def _running(board: str | None, count: int) -> None:
    """Put ``count`` tasks into ``running`` with a live claim on ``board``."""
    with kbc.connect_closing(board=board) as conn:
        for index in range(count):
            task_id = kb.create_task(conn, title=f"worker-{index}", assignee="worker")
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? WHERE id=?",
                (f"{kb._host_prefix()}1", 4242 + index, task_id),
            )
        conn.commit()


def test_status_reports_a_running_board_as_not_paused(client, kanban_home):
    _running(None, 2)

    payload = client.get(f"{PREFIX}/dispatch/status").json()

    assert payload["paused"] is False
    assert payload["state"] is None
    assert payload["running_count"] == 2


def test_all_boards_status_aggregates_pause_and_running_counts(client, kanban_home):
    kb.create_board("other-board")
    _running(None, 1)
    _running("other-board", 2)
    kbd.pause_dispatch("other-board", note="maintenance")

    response = client.get(f"{PREFIX}/dispatch/status?boards=*")

    assert response.status_code == 200
    payload = response.json()
    assert payload["board_count"] == 2
    assert payload["paused_count"] == 1
    assert payload["running_count"] == 3
    assert payload["all_paused"] is False
    assert {item["board"]: item["paused"] for item in payload["boards"]} == {
        "default": False,
        "other-board": True,
    }


def test_pause_then_status_reports_the_reason_and_drain_count(client, kanban_home):
    board = "drain-me"
    kb.create_board(board)
    _running(board, 3)

    paused = client.post(f"{PREFIX}/dispatch/pause?board={board}", json={"note": "gateway restart"})

    assert paused.status_code == 200
    assert paused.json()["paused"] is True

    status = client.get(f"{PREFIX}/dispatch/status?board={board}").json()

    assert status["paused"] is True
    assert status["state"]["reason"] == "operator_paused"
    assert status["state"]["note"] == "gateway restart"
    # The whole point of the control: pausing must not kill in-flight workers,
    # so the operator watches this fall to 0 before restarting.
    assert status["running_count"] == 3


def test_running_count_reaches_zero_as_paused_workers_finish(client, kanban_home):
    board = "drains-to-zero"
    kb.create_board(board)
    _running(board, 1)
    client.post(f"{PREFIX}/dispatch/pause?board={board}", json={})

    with kbc.connect_closing(board=board) as conn:
        task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
        assert kb.complete_task(conn, task_id, summary="drained") is True

    status = client.get(f"{PREFIX}/dispatch/status?board={board}").json()

    assert status["paused"] is True
    assert status["running_count"] == 0


def test_pause_is_board_scoped(client, kanban_home):
    """One board draining must never fence a sibling board's dispatch."""
    kb.create_board("paused-board")
    kb.create_board("busy-board")

    client.post(f"{PREFIX}/dispatch/pause?board=paused-board", json={})

    assert client.get(f"{PREFIX}/dispatch/status?board=paused-board").json()["paused"] is True
    assert client.get(f"{PREFIX}/dispatch/status?board=busy-board").json()["paused"] is False
    assert kbd.read_dispatch_pause("busy-board") is None


def test_pause_all_boards_fences_every_active_board(client, kanban_home):
    kb.create_board("other-board")

    response = client.post(f"{PREFIX}/dispatch/pause?boards=*", json={"note": "gateway restart"})

    assert response.status_code == 200
    assert response.json()["board_count"] == 2
    assert response.json()["paused_count"] == 2
    assert response.json()["failures"] == []
    default_pause = kbd.read_dispatch_pause("default")
    other_pause = kbd.read_dispatch_pause("other-board")
    assert default_pause is not None
    assert other_pause is not None
    assert default_pause["note"] == "gateway restart"
    assert other_pause["note"] == "gateway restart"


def test_resume_clears_the_pause_and_reports_the_previous_state(client, kanban_home):
    board = "resume-me"
    kb.create_board(board)
    client.post(f"{PREFIX}/dispatch/pause?board={board}", json={"note": "done now"})

    resumed = client.post(f"{PREFIX}/dispatch/resume?board={board}")

    assert resumed.status_code == 200
    assert resumed.json()["resumed"] is True
    assert resumed.json()["previous"]["reason"] == "operator_paused"
    assert client.get(f"{PREFIX}/dispatch/status?board={board}").json()["paused"] is False
    assert kbd.read_dispatch_pause(board) is None


def test_resume_all_boards_clears_every_active_board_pause(client, kanban_home):
    kb.create_board("other-board")
    kbd.pause_dispatch("default")
    kbd.pause_dispatch("other-board")

    response = client.post(f"{PREFIX}/dispatch/resume?boards=*")

    assert response.status_code == 200
    assert response.json()["board_count"] == 2
    assert response.json()["resumed_count"] == 2
    assert response.json()["failures"] == []
    assert kbd.read_dispatch_pause("default") is None
    assert kbd.read_dispatch_pause("other-board") is None


def test_pause_without_a_note_is_accepted(client, kanban_home):
    """The note is optional — an empty body must not 422."""
    board = "no-note"
    kb.create_board(board)

    response = client.post(f"{PREFIX}/dispatch/pause?board={board}")

    assert response.status_code == 200
    assert response.json()["state"]["reason"] == "operator_paused"


def test_status_surfaces_a_systemic_fault_pause_too(client, kanban_home):
    """The panel is the operator's one pause window — a fault must show there."""
    board = "faulted"
    kb.create_board(board)
    kbd._write_dispatch_pause(
        board,
        "restart_safe_scope_unavailable",
        fault_code="systemd_user_scope_unavailable",
        recovery="repair the user scope prerequisite, then resume explicitly",
    )

    status = client.get(f"{PREFIX}/dispatch/status?board={board}").json()

    assert status["paused"] is True
    assert status["state"]["reason"] == "restart_safe_scope_unavailable"
    assert "manual intervention required" in status["message"]


def test_status_message_renders_an_operator_pause_readably(client, kanban_home):
    board = "readable"
    kb.create_board(board)
    client.post(f"{PREFIX}/dispatch/pause?board={board}", json={"note": "2.7 rollout"})

    status = client.get(f"{PREFIX}/dispatch/status?board={board}").json()

    assert "paused for maintenance" in status["message"]
    assert "2.7 rollout" in status["message"]


def test_omitted_board_pauses_the_current_board_not_default(client, kanban_home):
    """The Desktop sends no ``board`` while the active board is selected.

    ``BoardSwitcher`` stores "currently active" as an empty slug, so the whole
    default UI path arrives here with the param omitted. If pause resolved that
    to ``default`` while status resolved it to the current board, the operator
    would press Pause, see the board stay unpaused, and restart the gateway on
    top of live workers.
    """
    kb.create_board("active-board")
    kb.set_current_board("active-board")
    _running("active-board", 1)

    # Control: status already resolves the omission through the current-board
    # pointer, so this count proves the three routes are compared on one board.
    assert client.get(f"{PREFIX}/dispatch/status").json()["running_count"] == 1

    paused = client.post(f"{PREFIX}/dispatch/pause", json={"note": "gateway restart"})

    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert client.get(f"{PREFIX}/dispatch/status").json()["paused"] is True
    assert kbd.read_dispatch_pause("active-board") is not None
    assert kbd.read_dispatch_pause("default") is None


def test_omitted_board_resumes_the_current_board_not_default(client, kanban_home):
    kb.create_board("active-board")
    kb.set_current_board("active-board")
    kbd.pause_dispatch("active-board", note="gateway restart")

    resumed = client.post(f"{PREFIX}/dispatch/resume")

    assert resumed.status_code == 200
    assert resumed.json()["was_paused"] is True
    assert kbd.read_dispatch_pause("active-board") is None
    assert client.get(f"{PREFIX}/dispatch/status").json()["paused"] is False


def test_an_explicit_board_still_wins_over_the_current_board(client, kanban_home):
    """Resolving the omission must not weaken explicit-board isolation."""
    kb.create_board("active-board")
    kb.create_board("other-board")
    kb.set_current_board("active-board")

    client.post(f"{PREFIX}/dispatch/pause?board=other-board", json={})

    assert kbd.read_dispatch_pause("other-board") is not None
    assert kbd.read_dispatch_pause("active-board") is None
    assert client.get(f"{PREFIX}/dispatch/status").json()["paused"] is False


def test_unknown_board_is_rejected_not_silently_applied_to_the_active_board(
    client, kanban_home,
):
    """A typo'd slug must not pause whatever board happens to be active."""
    kb.create_board("real-board")
    # Control: the same route with a REAL slug must succeed, so the 404 below
    # proves board validation, not a missing route (a nonexistent path 404s
    # identically).
    assert client.post(f"{PREFIX}/dispatch/pause?board=real-board", json={}).status_code == 200

    response = client.post(f"{PREFIX}/dispatch/pause?board=no-such-board", json={})

    assert response.status_code == 404
    assert kbd.read_dispatch_pause(None) is None
