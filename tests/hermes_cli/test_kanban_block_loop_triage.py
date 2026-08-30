"""The unblock-loop breaker must not be re-armed by triage automation.

Regression test for the ``block_loop_detected -> specified -> promoted ->
claimed -> blocked`` cycle.

``block_task`` routes a task that re-blocks for the same cause
``BLOCK_RECURRENCE_LIMIT`` times into ``triage``, so a human can decide what
to do with it. But ``triage`` is also the input queue for both triage
automations:

* ``kanban_specify.list_triage_ids``   (``hermes kanban specify --all``)
* ``kanban_decompose.list_triage_ids`` (the gateway auto-decompose sweep)

If either hands a loop-broken task back to its specifier/decomposer, the task
is promoted to ``ready``, claimed, blocks again on the same unsatisfiable
cause, and returns to triage — every dispatcher tick, forever. On a real board
this reached ``recurrences: 18`` against a limit of 2, burning a worker slot
and real tokens on a ~45s cycle.

These tests drive the real ``block_task`` transitions rather than writing
``block_recurrences`` by hand, so they assert the actual contract between the
breaker and the sweeps: **whatever the breaker parks, the sweeps must skip.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _drive_into_loop_breaker(conn, task_id: str, *, kind: str = "needs_input") -> None:
    """Re-block a task for the same cause until the breaker trips.

    Mirrors what actually happens on a board: a worker blocks, something
    unblocks the task, the next worker hits the identical wall. ``block_task``
    only counts a recurrence when it fires from ``running``/``ready``, i.e.
    after an unblock returned the task to the work pool.
    """
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
        kb.block_task(conn, task_id, reason="same unsatisfiable cause", kind=kind)
        kb.unblock_task(conn, task_id)
    kb.block_task(conn, task_id, reason="same unsatisfiable cause", kind=kind)


def test_block_task_parks_repeat_offender_in_triage(kanban_home):
    """Precondition: the breaker really does route to triage, not blocked."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unsatisfiable", assignee="worker")
        _drive_into_loop_breaker(conn, tid)

        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT


@pytest.mark.parametrize(
    "list_triage_ids",
    [spec.list_triage_ids, decomp.list_triage_ids],
    ids=["specify", "decompose"],
)
def test_triage_sweeps_skip_loop_broken_tasks(kanban_home, list_triage_ids):
    """A task parked by the breaker must be invisible to BOTH sweeps.

    Otherwise the automation re-promotes it and the loop restarts.
    """
    with kb.connect_closing() as conn:
        looped = kb.create_task(conn, title="unsatisfiable", assignee="worker")
        _drive_into_loop_breaker(conn, looped)

    assert looped not in list_triage_ids()


@pytest.mark.parametrize(
    "list_triage_ids",
    [spec.list_triage_ids, decomp.list_triage_ids],
    ids=["specify", "decompose"],
)
def test_triage_sweeps_still_return_ordinary_triage_tasks(
    kanban_home, list_triage_ids
):
    """The fix must not starve the sweeps of legitimate work.

    A normal triage card (a rough idea filed by a human) has no block history
    and must still be picked up.
    """
    with kb.connect_closing() as conn:
        fresh = kb.create_task(conn, title="rough idea", triage=True)
        assert kb.get_task(conn, fresh).status == "triage"

    assert fresh in list_triage_ids()


@pytest.mark.parametrize(
    "list_triage_ids",
    [spec.list_triage_ids, decomp.list_triage_ids],
    ids=["specify", "decompose"],
)
def test_sweeps_return_fresh_and_skip_looped_together(kanban_home, list_triage_ids):
    """Both kinds of card coexist in triage; only the looped one is skipped."""
    with kb.connect_closing() as conn:
        fresh = kb.create_task(conn, title="rough idea", triage=True)
        looped = kb.create_task(conn, title="unsatisfiable", assignee="worker")
        _drive_into_loop_breaker(conn, looped)

    ids = list_triage_ids()
    assert fresh in ids
    assert looped not in ids


def test_task_below_limit_is_not_skipped(kanban_home):
    """Only tasks at/over the limit are parked.

    A task that blocked once and was unblocked has a recurrence count below
    the limit — it is ordinary work, not a loop, and must not be filtered.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="blocked once", assignee="worker")
        kb.block_task(conn, tid, reason="one off", kind="needs_input")
        kb.unblock_task(conn, tid)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.block_recurrences < kb.BLOCK_RECURRENCE_LIMIT
        assert task.status != "triage"
