"""Deliberate operator dispatch pause for maintenance drain.

The board owner needs to restart ``hermes-gateway.service`` without SIGKILLing
in-flight workers (they share the gateway's ``KillMode=mixed`` cgroup, so a
restart discards uncommitted worktree progress). The existing durable pause
circuit is the right primitive; these are the behaviour contracts for setting
it deliberately rather than only via a systemic fault.

Contracts under test:

* ``pause_dispatch`` stops NEW claims/spawns but never touches a worker that is
  already running, and a running worker can still complete or block while the
  board is paused.
* It shares ``resume_dispatch``'s locking discipline: a contended board refuses
  rather than racing a live tick (which could clobber a fault pause the tick
  just wrote).
* It is idempotent — a second pause does not overwrite the first one's note or
  timestamp, so the "why is this paused" record is stable.
* ``resume_dispatch`` re-enables claiming.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


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


def test_operator_pause_drains_without_touching_running_workers(
    kanban_home, all_assignees_spawnable,
):
    """The whole maintenance workflow, end to end.

    Spawn one worker, pause, prove the next tick claims nothing new while the
    running card is untouched, prove that card can still finish normally (the
    drain), then resume and prove claiming works again.
    """
    board = "maintenance-drain"
    spawned: list[str] = []

    with kbc.connect_closing(board=board) as conn:
        first = kb.create_task(conn, title="already running", assignee="worker")
        second = kb.create_task(conn, title="must not start", assignee="worker")
        conn.commit()
        started = kbd.dispatch_once(
            conn, board=board, max_spawn=1,
            spawn_fn=lambda task, *_a, **_k: spawned.append(task.id) or 4242,
        )

    assert [task_id for task_id, _who, _ws in started.spawned] == [first]
    assert spawned == [first]

    paused = kbd.pause_dispatch(board, note="gateway restart for 2.7 rollout")

    assert paused["paused"] is True
    assert paused["state"]["reason"] == "operator_paused"
    assert paused["state"]["note"] == "gateway restart for 2.7 rollout"

    # The pause must fence NEW work only. The already-running worker keeps its
    # claim, its pid, and its run — pausing is not a kill switch.
    with kbc.connect_closing(board=board) as conn:
        blocked_tick = kbd.dispatch_once(
            conn, board=board,
            spawn_fn=lambda *_a, **_k: pytest.fail("paused board must not spawn"),
        )
        running = kb.get_task(conn, first)
        queued = kb.get_task(conn, second)

    assert blocked_tick.spawned == []
    assert blocked_tick.dispatch_paused is not None
    assert blocked_tick.dispatch_paused["reason"] == "operator_paused"
    assert running is not None and running.status == "running"
    assert running.worker_pid == 4242
    assert running.claim_lock is not None
    assert queued is not None and queued.status == "ready"
    assert queued.consecutive_failures == 0

    # The drain: an in-flight worker still reports its own outcome while paused.
    with kbc.connect_closing(board=board) as conn:
        assert kb.complete_task(conn, first, summary="finished during drain") is True
        drained = kb.get_task(conn, first)
        stats = kb.board_stats(conn)

    assert drained is not None and drained.status == "done"
    assert stats["by_status"].get("running", 0) == 0  # safe to restart now

    assert kbd.resume_dispatch(board)["resumed"] is True
    with kbc.connect_closing(board=board) as conn:
        resumed = kbd.dispatch_once(
            conn, board=board,
            spawn_fn=lambda task, *_a, **_k: spawned.append(task.id) or 4243,
        )

    assert [task_id for task_id, _who, _ws in resumed.spawned] == [second]


def test_paused_board_still_accepts_a_worker_block(kanban_home, all_assignees_spawnable):
    """Blocking is the other terminal outcome a draining worker may report."""
    board = "drain-via-block"
    with kbc.connect_closing(board=board) as conn:
        task_id = kb.create_task(conn, title="asks a question", assignee="worker")
        conn.commit()
        kbd.dispatch_once(conn, board=board, spawn_fn=lambda *_a, **_k: 4242)

    kbd.pause_dispatch(board)

    with kbc.connect_closing(board=board) as conn:
        assert kb.block_task(conn, task_id, reason="needs a decision", kind="needs_input") is True
        task = kb.get_task(conn, task_id)
        stats = kb.board_stats(conn)

    assert task is not None and task.status == "blocked"
    assert stats["by_status"].get("running", 0) == 0


def test_pause_refuses_while_a_dispatch_tick_holds_the_board_lock(kanban_home):
    """Same discipline as ``resume_dispatch``: never race a live tick.

    A tick that is mid-flight may be about to write a fault pause of its own;
    an operator pause landing inside that window would be overwritten (or would
    overwrite it) with no record of either.
    """
    board = "pause-lock-race"
    db_path = kb.kanban_db_path(board=board)

    with kbc._dispatch_tick_lock(db_path) as held:
        assert held is True
        refused = kbd.pause_dispatch(board, note="during a tick")

    assert refused == {"paused": False, "state": None, "reason": "dispatch_in_progress"}
    assert kbd.read_dispatch_pause(board) is None


def test_pause_is_idempotent_and_keeps_the_original_reason_record(kanban_home):
    """Re-pausing must not clobber the first note or restamp the pause time."""
    board = "double-pause"
    first = kbd.pause_dispatch(board, note="draining for kernel upgrade")
    again = kbd.pause_dispatch(board, note="a later, less informative note")

    assert again["paused"] is True
    assert again["state"] == first["state"]
    assert again["state"]["note"] == "draining for kernel upgrade"
    assert kbd.read_dispatch_pause(board) == first["state"]


def test_operator_pause_never_hijacks_a_systemic_fault_circuit(kanban_home):
    """A fault pause outranks an operator one — recovery guidance must survive."""
    board = "fault-then-operator"
    fault = kbd._write_dispatch_pause(
        board,
        "restart_safe_scope_unavailable",
        fault_code="systemd_user_scope_unavailable",
        recovery="repair the user scope prerequisite, then resume explicitly",
    )

    result = kbd.pause_dispatch(board, note="maintenance")

    assert result["paused"] is True
    assert result["state"] == fault
    assert kbd.read_dispatch_pause(board) == fault


def test_pause_state_records_who_and_when(kanban_home):
    board = "attribution"
    state = kbd.pause_dispatch(board)["state"]

    assert state["reason"] == "operator_paused"
    assert isinstance(state["paused_at"], int) and state["paused_at"] > 0
    assert state["paused_by"]
    assert "note" not in state or state["note"] is None


def test_operator_pause_message_is_human_and_names_the_resume_command(kanban_home):
    """The generic fallback reads like a fault; an operator pause is not one."""
    board = "readable"
    state = kbd.pause_dispatch(board, note="gateway restart")["state"]

    message = kbd.dispatch_pause_message(state, board=board)

    assert "paused for maintenance" in message
    assert "gateway restart" in message
    assert f"hermes kanban --board {board} dispatch --resume-circuit" in message
    # It must NOT read as a systemic failure needing repair.
    assert "manual intervention required" not in message


def test_operator_pause_survives_a_process_restart(kanban_home):
    """The point of the feature: the pause outlives the gateway restart."""
    import subprocess
    import sys

    board = "survives-restart"
    state = kbd.pause_dispatch(board, note="restarting the gateway")["state"]

    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env["HERMES_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_HOME"] = str(kb.kanban_home())
    script = f"""
import json
from hermes_cli import kanban_db_dispatch as kbd
print(json.dumps(kbd.read_dispatch_pause({board!r})))
"""
    restarted = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True,
    )

    assert json.loads(restarted.stdout) == state


# --- CLI -------------------------------------------------------------------


def _dispatch_args(**overrides) -> argparse.Namespace:
    base = dict(
        board=None, dry_run=False, max=None, failure_limit=2, json=False,
        resume_circuit=False, circuit_status=False, pause=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_pause_verb_sets_the_circuit_with_the_typed_note(kanban_home, capsys):
    from hermes_cli import kanban_ops

    board = "cli-pause"
    args = _dispatch_args(board=board, pause=["draining", "for", "maintenance"])

    assert kanban_ops._cmd_dispatch(args) == 0

    state = kbd.read_dispatch_pause(board)
    assert state is not None
    assert state["reason"] == "operator_paused"
    assert state["note"] == "draining for maintenance"
    assert "paused for maintenance" in capsys.readouterr().out


def test_cli_pause_supports_json_like_the_other_circuit_verbs(kanban_home, capsys):
    from hermes_cli import kanban_ops

    board = "cli-pause-json"
    assert kanban_ops._cmd_dispatch(_dispatch_args(board=board, pause=[], json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["paused"] is True
    assert payload["state"]["reason"] == "operator_paused"


def test_cli_pause_reports_failure_when_a_tick_owns_the_lock(kanban_home, capsys):
    """Automation must not read a refused pause as a drained board."""
    from hermes_cli import kanban_ops

    board = "cli-pause-contended"
    db_path = kb.kanban_db_path(board=board)

    with kbc._dispatch_tick_lock(db_path) as held:
        assert held is True
        code = kanban_ops._cmd_dispatch(_dispatch_args(board=board, pause=[]))

    assert code == 1
    assert kbd.read_dispatch_pause(board) is None
