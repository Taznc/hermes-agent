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
from hermes_cli import kanban_db_dispatch_postdrain as pd

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


# --- post-drain action queue ------------------------------------------------


@pytest.fixture
def allowlisted(monkeypatch):
    """One unit is restartable; the request body may only name THAT unit."""
    monkeypatch.setattr(
        pd, "resolve_post_drain_config",
        lambda: pd.PostDrainConfig(
            service_restart_allowlist=("hermes-gateway.service",),
            service_restart_scope="system",
            default_expiry_seconds=3600,
            max_expiry_seconds=86400,
        ),
    )


def test_status_reports_no_queued_action_by_default(client, kanban_home):
    assert client.get(f"{PREFIX}/dispatch/status").json()["post_drain"] is None


def test_queueing_an_action_surfaces_it_on_status_with_the_live_running_count(
    client, kanban_home,
):
    """The panel renders action + running count + time remaining from one poll."""
    board = "queue-me"
    kb.create_board(board)
    _running(board, 3)
    client.post(f"{PREFIX}/dispatch/pause?board={board}", json={})

    queued = client.post(
        f"{PREFIX}/dispatch/post-drain?board={board}",
        json={"action_kind": "reboot", "expires_in_seconds": 1800},
    )

    assert queued.status_code == 200
    assert queued.json()["queued"] is True

    status = client.get(f"{PREFIX}/dispatch/status?board={board}").json()

    assert status["post_drain"]["action_kind"] == "reboot"
    assert status["post_drain"]["state"] == "waiting"
    assert status["running_count"] == 3
    assert 0 < status["post_drain"]["expires_in_seconds"] <= 1800


def test_an_unknown_action_kind_is_rejected(client, kanban_home):
    response = client.post(f"{PREFIX}/dispatch/post-drain", json={"action_kind": "rm_rf"})

    assert response.status_code == 400
    assert client.get(f"{PREFIX}/dispatch/status").json()["post_drain"] is None


def test_a_target_outside_the_allowlist_is_rejected(client, kanban_home, allowlisted):
    response = client.post(
        f"{PREFIX}/dispatch/post-drain",
        json={"action_kind": "service_restart", "target": "sshd.service"},
    )

    assert response.status_code == 400
    assert client.get(f"{PREFIX}/dispatch/status").json()["post_drain"] is None

    # Control: the same route with an ALLOWLISTED unit succeeds, so the 400
    # above proves allowlist validation rather than a broken route.
    allowed = client.post(
        f"{PREFIX}/dispatch/post-drain",
        json={"action_kind": "service_restart", "target": "hermes-gateway.service"},
    )
    assert allowed.status_code == 200


def test_service_restart_is_rejected_when_no_allowlist_is_configured(client, kanban_home):
    """The shipped default configures no restartable unit at all."""
    response = client.post(
        f"{PREFIX}/dispatch/post-drain",
        json={"action_kind": "service_restart", "target": "hermes-gateway.service"},
    )

    assert response.status_code == 400


def test_cancelling_a_queued_action_clears_it_from_status(client, kanban_home):
    client.post(f"{PREFIX}/dispatch/post-drain", json={"action_kind": "reboot"})

    cancelled = client.delete(f"{PREFIX}/dispatch/post-drain")

    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    assert client.get(f"{PREFIX}/dispatch/status").json()["post_drain"]["state"] == "cancelled"


def test_resuming_dispatch_cancels_the_queued_action_over_rest(client, kanban_home):
    board = "changed-my-mind"
    kb.create_board(board)
    client.post(f"{PREFIX}/dispatch/pause?board={board}", json={})
    client.post(f"{PREFIX}/dispatch/post-drain?board={board}", json={"action_kind": "reboot"})

    client.post(f"{PREFIX}/dispatch/resume?board={board}")

    status = client.get(f"{PREFIX}/dispatch/status?board={board}").json()
    assert status["post_drain"]["state"] == "cancelled"


def test_the_queue_is_board_scoped(client, kanban_home):
    kb.create_board("queued-board")
    kb.create_board("untouched-board")

    client.post(f"{PREFIX}/dispatch/post-drain?board=queued-board", json={"action_kind": "reboot"})

    assert client.get(f"{PREFIX}/dispatch/status?board=queued-board").json()["post_drain"]
    assert client.get(f"{PREFIX}/dispatch/status?board=untouched-board").json()["post_drain"] is None


def test_an_unknown_board_is_rejected_by_the_queue_route(client, kanban_home):
    kb.create_board("real-board")
    assert client.post(
        f"{PREFIX}/dispatch/post-drain?board=real-board", json={"action_kind": "reboot"},
    ).status_code == 200

    response = client.post(
        f"{PREFIX}/dispatch/post-drain?board=no-such-board", json={"action_kind": "reboot"},
    )

    assert response.status_code == 404
    assert pd.read_post_drain_action(None) is None


def test_aggregate_queue_writes_one_record_per_board_sharing_a_group(client, kanban_home):
    kb.create_board("other-board")

    response = client.post(f"{PREFIX}/dispatch/post-drain?boards=*", json={"action_kind": "reboot"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["board_count"] == 2
    assert payload["queued_count"] == 2
    assert payload["failures"] == []
    groups = {pd.read_post_drain_action(slug)["group_id"] for slug in ("default", "other-board")}
    assert len(groups) == 1 and None not in groups


def test_aggregate_status_reports_the_queued_action_and_total_running(client, kanban_home):
    kb.create_board("other-board")
    _running(None, 2)
    client.post(f"{PREFIX}/dispatch/post-drain?boards=*", json={"action_kind": "reboot"})

    status = client.get(f"{PREFIX}/dispatch/status?boards=*").json()

    assert status["post_drain"]["action_kind"] == "reboot"
    assert status["post_drain"]["state"] == "waiting"
    assert status["running_count"] == 2
    assert {item["board"]: item["post_drain"]["state"] for item in status["boards"]} == {
        "default": "waiting",
        "other-board": "waiting",
    }


def test_aggregate_cancel_clears_every_board(client, kanban_home):
    kb.create_board("other-board")
    client.post(f"{PREFIX}/dispatch/post-drain?boards=*", json={"action_kind": "reboot"})

    response = client.delete(f"{PREFIX}/dispatch/post-drain?boards=*")

    assert response.status_code == 200
    assert response.json()["cancelled_count"] == 2
    for slug in ("default", "other-board"):
        assert pd.read_post_drain_action(slug)["state"] == "cancelled"


def test_the_available_action_kinds_are_advertised_for_the_selector(client, kanban_home, allowlisted):
    """The UI must render only what this host will actually accept."""
    payload = client.get(f"{PREFIX}/dispatch/status").json()

    assert payload["post_drain_actions"] == [
        {"action_kind": "service_restart", "targets": ["hermes-gateway.service"]},
        {"action_kind": "reboot", "targets": []},
    ]


def test_service_restart_is_not_offered_without_an_allowlist(client, kanban_home):
    payload = client.get(f"{PREFIX}/dispatch/status").json()

    assert payload["post_drain_actions"] == [{"action_kind": "reboot", "targets": []}]
