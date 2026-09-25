"""Fork-owned tests for ``hermes_fork.kanban.dependency_links``.

Extraction target: project-link resolution for ``create_task``, the
parent-dependency-satisfaction predicate and its deferred-skip-link
bookkeeping, and the roadmap-lane state machine (``on_hold`` /
``idea``/``roadmap`` transitions), moved out of ``hermes_cli.kanban_db``
behind the ``# >>> FORK ANCHOR: kanban-dependency-links <<<`` marker. See
``tests/hermes_cli/test_kanban_db.py`` for end-to-end dispatcher-integration
coverage of this same logic reached through the ``kanban_db`` facade; these
tests instead pin the extracted module's own contracts: the pure predicates
in isolation, and that the re-exported facade attributes are identity-equal
to the extracted functions (proving the anchor wires the fork module in
rather than duplicating behavior that could drift).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_fork.kanban import dependency_links as dl


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PIN_HOME", raising=False)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Re-export identity: the facade attribute IS the extracted function, not a
# copy — proves the anchor import wires the fork module in rather than
# duplicating behavior that could drift.
# ---------------------------------------------------------------------------


def test_kanban_db_reexports_the_extracted_lane_and_link_functions():
    assert kb.hold_task is dl.hold_task
    assert kb.unhold_task is dl.unhold_task
    assert kb.refine_task is dl.refine_task
    assert kb.demote_task is dl.demote_task
    assert kb.spawn_roadmap_task is dl.spawn_roadmap_task
    assert kb._parent_dependency_satisfied is dl._parent_dependency_satisfied
    assert kb.ROADMAP_LANE_TRANSITIONS is dl.ROADMAP_LANE_TRANSITIONS
    assert kb.ROADMAP_SPAWN_TARGETS is dl.ROADMAP_SPAWN_TARGETS


def test_extraction_late_bound_origin_resolves_to_the_real_module():
    """The cycle-breaking ``_kb`` module ref must point at the real,
    fully-initialized origin module, not a stand-in or partial import."""
    assert dl._kb is kb


# ---------------------------------------------------------------------------
# Pure predicates
# ---------------------------------------------------------------------------


def test_parent_dependency_satisfied_done_or_archived_with_completed_at():
    assert dl._parent_dependency_satisfied({"status": "done", "completed_at": None}) is True
    assert dl._parent_dependency_satisfied(
        {"status": "archived", "completed_at": 123}
    ) is True
    assert dl._parent_dependency_satisfied(
        {"status": "archived", "completed_at": None}
    ) is False
    assert dl._parent_dependency_satisfied({"status": "running", "completed_at": None}) is False


def test_validate_lane_rejects_combination_with_triage_or_nondefault_status():
    assert dl._validate_lane(None) is None
    assert dl._validate_lane("idea") == "idea"
    with pytest.raises(ValueError, match="one of"):
        dl._validate_lane("nonsense")
    with pytest.raises(ValueError, match="mutually exclusive"):
        dl._validate_lane("idea", triage=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        dl._validate_lane("idea", initial_status="blocked")


# ---------------------------------------------------------------------------
# Roadmap lane state machine
# ---------------------------------------------------------------------------


def test_lane_transitions_are_one_directional_out_of_the_wishlist(kanban_home):
    with kbc.connect_closing() as conn:
        idea_id = kb.create_task(conn, title="idea card", lane="idea")
        # idea -> roadmap via refine_task
        assert dl.refine_task(conn, idea_id) is True
        assert kb.get_task(conn, idea_id).status == "roadmap"
        # roadmap -> idea via demote_task
        assert dl.demote_task(conn, idea_id) is True
        assert kb.get_task(conn, idea_id).status == "idea"
        # idea cannot go straight to triage/ready (must be refined first)
        with pytest.raises(ValueError, match="invalid roadmap lane transition"):
            dl.spawn_roadmap_task(conn, idea_id, to="triage")


def test_spawn_roadmap_task_rejects_an_unlisted_target(kanban_home):
    with kbc.connect_closing() as conn:
        idea_id = kb.create_task(conn, title="idea card", lane="roadmap")
        with pytest.raises(ValueError, match="spawn target must be one of"):
            dl.spawn_roadmap_task(conn, idea_id, to="blocked")


def test_spawn_to_ready_is_parent_gated(kanban_home):
    """A roadmap card whose parent isn't done lands in ``todo``, not a bogus ``ready``."""
    def fake_spawn(task, workspace, board=None):  # pragma: no cover - must never be called
        raise AssertionError(f"dispatcher spawned a parent-gated card: {task.id}")

    with kbc.connect_closing() as conn:
        epic = kb.create_task(conn, title="epic", assignee="alice")
        child = kb.create_task(
            conn, title="gated child", assignee="alice", lane="roadmap", parents=(epic,),
        )
        assert dl.spawn_roadmap_task(conn, child, to="ready") is True
        assert kb.get_task(conn, child).status == "todo"


def test_hold_and_unhold_round_trip_and_reset_failure_streak(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        assert dl.hold_task(conn, tid, reason="pausing") is True
        assert kb.get_task(conn, tid).status == "on_hold"
        # A live status refuses hold_task's own precondition a second time.
        assert dl.hold_task(conn, tid) is False

        assert dl.unhold_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_unhold_is_a_noop_when_not_on_hold(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        assert dl.unhold_task(conn, tid) is False


# ---------------------------------------------------------------------------
# Deferred skip-link bookkeeping
# ---------------------------------------------------------------------------


def test_born_satisfied_parents_only_archived_and_completed(kanban_home):
    with kbc.connect_closing() as conn:
        done_then_archived = kb.create_task(conn, title="done", assignee="alice")
        kb.complete_task(conn, done_then_archived, summary="ok")
        kb.archive_task(conn, done_then_archived)

        withdrawn = kb.create_task(conn, title="withdrawn", assignee="alice")
        kb.archive_task(conn, withdrawn)

        running = kb.create_task(conn, title="running", assignee="alice")

        satisfied = dl._born_satisfied_parents(conn, [done_then_archived, withdrawn, running])
        assert satisfied == {done_then_archived}


def test_pending_skipped_links_and_restore_round_trip(kanban_home):
    """Linking a child under an already-archived-and-completed parent is skipped
    (never materialized); reopening the parent restores it."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        kb.complete_task(conn, parent, summary="done")
        kb.archive_task(conn, parent)

        child = kb.create_task(conn, title="child", assignee="alice", parents=(parent,))

        # The edge was never materialized in task_links.
        row = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child),
        ).fetchone()
        assert row is None
        assert (parent, child) in dl._pending_skipped_links(conn, parent_id=parent)

        restored = dl._restore_skipped_child_links(conn, parent)
        assert restored == [child]
        row = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child),
        ).fetchone()
        assert row is not None
        # Now settled: no longer pending.
        assert dl._pending_skipped_links(conn, parent_id=parent) == []


def test_clear_satisfied_outgoing_links_drops_edges_from_a_completed_archived_parent(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="alice", parents=(parent,))
        # Link exists while the parent is still live.
        row = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child),
        ).fetchone()
        assert row is not None

        kb.complete_task(conn, parent, summary="done")
        removed = dl._clear_satisfied_outgoing_links(conn, parent)
        assert removed == [child]
        row = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child),
        ).fetchone()
        assert row is None
