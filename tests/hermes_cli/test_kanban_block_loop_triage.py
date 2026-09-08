"""The unblock-loop breaker must not be re-armed by triage automation, but
only for a genuine human-decision gate.

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

That reach-the-limit condition is necessary but not sufficient to exclude a
task from the sweeps: decomposition is a legitimate remedy for a genuine
scope/fanout loop (a ``capability`` or ``transient`` block that a tighter
spec might actually resolve), and blanket-excluding those starves the sweeps
of real work. Only a ``needs_input`` loop — "a human has not decided yet" —
is unresolvable by re-specifying/re-decomposing, so the exclusion is scoped
to ``block_kind == "needs_input"`` AND the recurrence limit, not either alone.

These tests drive the real ``block_task`` transitions rather than writing
``block_recurrences``/``block_kind`` by hand, so they assert the actual
contract between the breaker and the sweeps: **whatever the breaker parks
for an unresolved human decision, the sweeps must skip — but a loop-broken
task with a different cause stays eligible.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
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
    with kbc.connect_closing() as conn:
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
    with kbc.connect_closing() as conn:
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
    with kbc.connect_closing() as conn:
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
    with kbc.connect_closing() as conn:
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
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="blocked once", assignee="worker")
        kb.block_task(conn, tid, reason="one off", kind="needs_input")
        kb.unblock_task(conn, tid)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.block_recurrences < kb.BLOCK_RECURRENCE_LIMIT
        assert task.status != "triage"


@pytest.mark.parametrize(
    "list_triage_ids",
    [spec.list_triage_ids, decomp.list_triage_ids],
    ids=["specify", "decompose"],
)
@pytest.mark.parametrize("kind", ["capability", "transient"])
def test_non_needs_input_loop_stays_decomposable(kanban_home, list_triage_ids, kind):
    """AC3: a loop that is NOT a human-decision gate (a real scope/fanout problem)
    must still be visible to the sweeps once it hits the recurrence limit.

    Only ``needs_input`` means "a human has not decided yet" — the other block
    kinds a task can reach ``triage`` through are candidates a tightened spec
    might actually resolve, so decomposition must not be blanket-refused to
    every loop-broken task regardless of cause.
    """
    with kbc.connect_closing() as conn:
        looped = kb.create_task(conn, title="genuinely mis-scoped", assignee="worker")
        _drive_into_loop_breaker(conn, looped, kind=kind)

        task = kb.get_task(conn, looped)
        assert task is not None
        assert task.status == "triage"
        assert task.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT
        assert task.block_kind == kind

    assert looped in list_triage_ids()


def test_auto_decompose_tick_does_not_respecify_needs_input_loop(kanban_home, monkeypatch):
    """AC2: one dispatcher tick must not emit ``specified``/``promoted`` for a
    task the breaker parked on an unresolved human decision.

    Drives the real gateway sweep entry point (``auto_decompose_tick``) rather
    than only ``list_triage_ids()``. In the isolated test HOME there is no aux
    LLM client, so ``decompose_task`` normally fails outright (``ok=False``,
    "auxiliary client unavailable") BEFORE it ever reaches ``specify_triage_task``
    — meaning ``decomposed == 0`` and no events would hold trivially even if the
    triage-candidate query wrongly included the looped task. That would make
    this test vacuous: it could not fail if the exclusion regressed.

    To make the guard meaningful, ``_call_aux`` is patched to return a valid
    ``fanout=false`` reply, so *if* the looped task were ever handed to
    ``decompose_task`` it would genuinely succeed (``specify_triage_task`` ->
    ``triage -> todo`` -> ``recompute_ready`` -> ``todo -> ready``, emitting
    ``specified`` then ``promoted``). With that stub in place, ``decomposed == 0``
    and an empty event set can only mean the exclusion kept the task out of
    ``list_triage_ids()`` in the first place — not that the aux call failed.
    """
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        looped = kb.create_task(conn, title="unsatisfiable", assignee="worker")
        _drive_into_loop_breaker(conn, looped)

    def _fake_call_aux(*args, **kwargs):
        return (
            '{"fanout": false, "title": "unsatisfiable (re-specified)", '
            '"body": "still blocked on the same needs_input cause"}',
            "",
        )

    monkeypatch.setattr(decomp, "_call_aux", _fake_call_aux)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    settings = _DispatcherSettings(
        interval=60.0, max_spawn=None, max_in_progress=None, failure_limit=3,
        stale_timeout_seconds=3600, reconcile_orphans=False, default_assignee=None,
        default_reviewer=None, max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kb, settings=settings)
    decomposed = dispatcher.auto_decompose_tick(auto_decompose_per_tick=10)

    assert decomposed == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, looped)
        assert task is not None
        assert task.status == "triage"
        kinds = {e.kind for e in kb.list_events(conn, looped)}
    assert "specified" not in kinds
    assert "promoted" not in kinds
    assert "decomposed" not in kinds


def test_auto_decompose_tick_would_promote_if_exclusion_regressed(kanban_home, monkeypatch):
    """Mutation proof for the AC2 guard above.

    Forces the exact broken behaviour this card fixes — ``list_triage_ids()``
    handing the loop-broken task straight back to the sweep — and shows that,
    with a working aux stub, one tick genuinely re-specifies and promotes it.
    This is what makes the previous test's ``decomposed == 0`` assertion
    non-vacuous: it can only pass when the exclusion is actually in effect.
    """
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        looped = kb.create_task(conn, title="unsatisfiable", assignee="worker")
        _drive_into_loop_breaker(conn, looped)

    def _fake_call_aux(*args, **kwargs):
        return (
            '{"fanout": false, "title": "unsatisfiable (re-specified)", '
            '"body": "still blocked on the same needs_input cause"}',
            "",
        )

    monkeypatch.setattr(decomp, "_call_aux", _fake_call_aux)
    # Simulate the pre-fix candidate query: ignore the exclusion entirely.
    monkeypatch.setattr(decomp, "list_triage_ids", lambda **kw: [looped])
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    settings = _DispatcherSettings(
        interval=60.0, max_spawn=None, max_in_progress=None, failure_limit=3,
        stale_timeout_seconds=3600, reconcile_orphans=False, default_assignee=None,
        default_reviewer=None, max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kb, settings=settings)
    decomposed = dispatcher.auto_decompose_tick(auto_decompose_per_tick=10)

    assert decomposed == 1
    with kbc.connect_closing() as conn:
        kinds = {e.kind for e in kb.list_events(conn, looped)}
    assert "specified" in kinds
    assert "promoted" in kinds
