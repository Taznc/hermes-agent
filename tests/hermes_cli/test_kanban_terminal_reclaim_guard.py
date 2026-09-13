"""Tests: a card with a terminal ``completed`` event and no recorded reopen must
never be re-dispatched, even when ``tasks.status`` desyncs back to ``ready``
without a corresponding reopen event (t_e3b0913f).

Measured on the live board: 26% of all worker runs were re-dispatches of
already-``done`` cards (one card ran 25x). Root cause traced to at least one
concrete mechanism (a sandboxed probe leaking to the live board and running an
unqualified ``UPDATE tasks SET status='ready'``, t_029c5ee7) plus at least one
other still-unexplained desync (``t_07631dae`` reverted silently to ``ready``
*after* that leak was fixed and re-dispatched 10 hours later with zero events
between its ``completed`` and the next ``claimed``). Rather than chase every
possible writer of a bare status flip, ``claim_task`` now treats the event log
as authoritative: any ``ready`` row whose last ``completed`` event has no
``status``/``descendant_invalidated`` event after it is a desync, not a reopen,
and is healed back to ``done`` instead of spawning a duplicate worker.
"""

from __future__ import annotations

import json
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
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _force_ready_no_event(conn, task_id: str) -> None:
    """Simulate the desync class this card is about: something (a raw-SQL
    repair script, a leaked sandbox probe, a stale reclaim race) flips
    ``tasks.status`` back to ``ready`` WITHOUT going through any sanctioned
    reopen path — so no ``status``/``descendant_invalidated`` event is ever
    appended. This is deliberately raw SQL: it is the shape of the bug, not
    a call to a kanban_db mutator (every real mutator appends an event).
    """
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (task_id,),
    )
    conn.commit()


def test_completed_task_desynced_to_ready_is_not_reclaimed(conn):
    """The reproduction: a done card desynced back to 'ready' with no reopen
    event must not be claimed — it must self-heal back to 'done' instead."""
    tid = kb.create_task(conn, title="finished work", assignee="w")
    claimed = kb.claim_task(conn, tid, claimer="host:A")
    assert claimed is not None
    assert kb.complete_task(conn, tid, summary="done")
    assert kb.get_task(conn, tid).status == "done"

    _force_ready_no_event(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"  # desync reproduced

    result = kb.claim_task(conn, tid, claimer="host:B")

    assert result is None, "a desynced completed card must not be claimable"
    task = kb.get_task(conn, tid)
    assert task.status == "done", "claim_task must heal the desync back to done"
    assert task.claim_lock is None
    assert task.worker_pid is None

    kinds = [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
    ]
    assert "terminal_reclaim_rejected" in kinds
    # No second 'claimed'/'spawned' pair — the desync did not spawn a worker.
    assert kinds.count("claimed") == 1


def test_dashboard_reopen_to_ready_is_still_claimable(conn):
    """A LEGITIMATE reopen (the dashboard's real ``done -> ready`` drag path,
    ``_set_status_direct``'s shape: an explicit 'status' event recorded in the
    same transaction) must still be claimable — the guard must discriminate a
    real reopen from a silent desync, not block every done->ready card."""
    tid = kb.create_task(conn, title="reopened via dashboard", assignee="w")
    kb.claim_task(conn, tid, claimer="host:A")
    kb.complete_task(conn, tid, summary="done")
    assert kb.get_task(conn, tid).status == "done"

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "status", {"status": "ready", "requested_status": "ready"})

    result = kb.claim_task(conn, tid, claimer="host:B")

    assert result is not None, "a genuinely reopened card must remain claimable"
    assert result.status == "running"


def test_explicit_unarchive_to_ready_is_still_claimable(conn):
    tid = kb.create_task(conn, title="reopened from archive", assignee="w")
    assert kb.claim_task(conn, tid, claimer="host:A") is not None
    assert kb.complete_task(conn, tid, summary="done") is True
    assert kb.archive_task(conn, tid) is True
    assert kb.unarchive_task(conn, tid, status="ready") is True

    result = kb.claim_task(conn, tid, claimer="host:B")

    assert result is not None
    assert result.status == "running"


def test_review_reopen_path_unaffected_by_terminal_guard(conn):
    """A task that was never completed (plain ready -> running) is untouched
    by the new guard — no 'completed' event exists at all."""
    tid = kb.create_task(conn, title="never completed", assignee="w")
    claimed = kb.claim_task(conn, tid, claimer="host:A")
    assert claimed is not None
    assert claimed.status == "running"


# --- recompute_ready: the promotion seam (t_9e93afb1) ---
#
# ``claim_task`` above is the LAST line of defense, not the first. The
# dispatcher calls ``recompute_ready`` before it ever tries to claim, and a
# terminal card raw-desynced to 'todo'/'blocked' was promoted to 'ready' with a
# misleading ``promoted`` event. That promotion is not merely cosmetic: the
# dispatcher's ``_terminal_card_replay_ids`` scan then sees a 'ready' row with a
# terminal completion and trips a BOARD-WIDE pause, so ``claim_task``'s heal is
# never reached and every other card stops dispatching until a human runs
# ``--resume-circuit``.


def _force_todo_no_event(conn, task_id: str) -> None:
    """The promotion-seam shape of the desync: a raw writer flips a done card
    back to 'todo' without any sanctioned reopen event (see
    ``_force_ready_no_event`` for the claim-seam shape)."""
    conn.execute(
        "UPDATE tasks SET status = 'todo', claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (task_id,),
    )
    conn.commit()


def _events(conn, task_id: str) -> list:
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()


def test_recompute_ready_does_not_promote_terminal_card_desynced_to_todo(conn):
    """The reproduction at the promotion seam: a done card raw-desynced to
    'todo' must be healed back to 'done', not promoted to 'ready'."""
    tid = kb.create_task(conn, title="finished work", assignee="w")
    assert kb.claim_task(conn, tid, claimer="host:A") is not None
    assert kb.complete_task(conn, tid, summary="done")
    _force_todo_no_event(conn, tid)
    assert kb.get_task(conn, tid).status == "todo"  # desync reproduced

    promoted = kb.recompute_ready(conn)

    assert promoted == 0, "a terminal card must not count as a promotion"
    task = kb.get_task(conn, tid)
    assert task.status == "done", "recompute_ready must heal the desync back to done"
    assert task.claim_lock is None
    assert task.claim_expires is None
    assert task.worker_pid is None

    rows = _events(conn, tid)
    kinds = [r["kind"] for r in rows]
    assert "promoted" not in kinds, "no misleading promoted event may be recorded"
    rejections = [r for r in rows if r["kind"] == "terminal_reclaim_rejected"]
    assert len(rejections) == 1, "exactly one auditable terminal rejection"
    payload = json.loads(rejections[0]["payload"])
    assert payload["source"] == "recompute_ready"
    last_completed_id = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed' "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()["id"]
    assert payload["completed_event_id"] == last_completed_id
    assert payload["reason"]


def test_recompute_ready_does_not_promote_terminal_card_desynced_to_blocked(conn):
    """Same hole via the other status ``recompute_ready`` scans."""
    tid = kb.create_task(conn, title="finished work", assignee="w")
    assert kb.claim_task(conn, tid, claimer="host:A") is not None
    assert kb.complete_task(conn, tid, summary="done")
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    conn.commit()

    promoted = kb.recompute_ready(conn)

    assert promoted == 0
    assert kb.get_task(conn, tid).status == "done"
    kinds = [r["kind"] for r in _events(conn, tid)]
    assert "promoted" not in kinds
    assert kinds.count("terminal_reclaim_rejected") == 1


def test_recompute_ready_promotes_a_genuinely_reopened_card(conn):
    """Control: an explicit reopen is a sanctioned path off 'done' and must
    still promote normally — the guard discriminates, it does not blanket-heal
    every completed card back to done."""
    tid = kb.create_task(conn, title="reopened on purpose", assignee="w")
    assert kb.claim_task(conn, tid, claimer="host:A") is not None
    assert kb.complete_task(conn, tid, summary="done")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "status", {"status": "todo", "requested_status": "todo"})

    promoted = kb.recompute_ready(conn)

    assert promoted == 1
    assert kb.get_task(conn, tid).status == "ready"
    kinds = [r["kind"] for r in _events(conn, tid)]
    assert "promoted" in kinds
    assert "terminal_reclaim_rejected" not in kinds


def test_recompute_ready_leaves_parent_gated_cards_in_todo(conn):
    """Control: ordinary parent gating is unchanged — an unfinished parent
    still holds a never-completed child in 'todo' with no promotion and no
    terminal rejection."""
    parent = kb.create_task(conn, title="parent", assignee="w")
    child = kb.create_task(conn, title="child", assignee="w", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"

    assert kb.recompute_ready(conn) == 0

    assert kb.get_task(conn, child).status == "todo"
    kinds = [r["kind"] for r in _events(conn, child)]
    assert "promoted" not in kinds
    assert "terminal_reclaim_rejected" not in kinds

    assert kb.claim_task(conn, parent, claimer="host:A") is not None
    assert kb.complete_task(conn, parent, summary="done")
    # The parent's completion re-runs promotion for the child.
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"


def test_dispatch_tick_over_desynced_terminal_card_does_not_pause_the_board(
    conn, all_assignees_spawnable,
):
    """The system-level consequence: before the fix, ``recompute_ready``
    promotes the desynced card to 'ready' and ``_terminal_card_replay_ids``
    then trips a board-wide ``terminal_card_replay`` pause, stopping dispatch
    for every other card. The heal must happen inside ``recompute_ready`` so
    the row is already 'done' by the time that scan runs."""
    spawned_pids = []

    def _spawn(_task, _workspace, board=None):
        spawned_pids.append(_task.id)
        return 43210

    tid = kb.create_task(conn, title="finished work", assignee="w")
    assert kb.claim_task(conn, tid, claimer="host:A") is not None
    assert kb.complete_task(conn, tid, summary="done")
    other = kb.create_task(conn, title="unrelated queued work", assignee="w")
    _force_todo_no_event(conn, tid)

    result = kbd.dispatch_once(conn, spawn_fn=_spawn)

    assert result.dispatch_paused is None, (
        "a desynced terminal card must not halt the whole board"
    )
    assert kb.get_task(conn, tid).status == "done"
    assert tid not in spawned_pids, "the terminal card must not be re-dispatched"
    assert other in spawned_pids, "unrelated work must keep dispatching"

