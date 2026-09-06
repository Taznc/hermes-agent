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

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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


def test_review_reopen_path_unaffected_by_terminal_guard(conn):
    """A task that was never completed (plain ready -> running) is untouched
    by the new guard — no 'completed' event exists at all."""
    tid = kb.create_task(conn, title="never completed", assignee="w")
    claimed = kb.claim_task(conn, tid, claimer="host:A")
    assert claimed is not None
    assert claimed.status == "running"
