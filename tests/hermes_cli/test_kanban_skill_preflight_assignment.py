"""Batch B: every assignee mutation preflights the TARGET profile, and does so
before anything destructive or state-changing happens."""
from __future__ import annotations

import pytest

from tests.hermes_cli.test_kanban_skill_preflight import (  # noqa: F401
    _make_profile, kanban_home,
)


def _state(kanban_db, conn, task_id):
    """The fields a refused mutation must leave byte-identical."""
    task = kanban_db.get_task(conn, task_id)
    return {
        "status": task.status,
        "assignee": task.assignee,
        "claim_lock": task.claim_lock,
        "events": [e.kind for e in kanban_db.list_events(conn, task_id)],
    }


def test_reassign_validates_the_target_before_reclaiming_the_running_worker(kanban_home):
    """``reassign --reclaim`` terminates a live claim. Doing that first and
    THEN discovering the target profile cannot load the card's skills leaves
    the board strictly worse off than before the call: the worker is gone and
    the card is back in ready with its old, working assignee."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import KanbanSkillPreflightError

    _make_profile(kanban_home, "alpha", ["github-code-review"])
    _make_profile(kanban_home, "beta", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="card", assignee="alpha", skills=["github-code-review"],
        )
        assert kanban_db.claim_task(conn, task_id) is not None
        before = _state(kanban_db, conn, task_id)
        assert before["status"] == "running" and before["claim_lock"] is not None

        with pytest.raises(KanbanSkillPreflightError):
            kanban_db.reassign_task(conn, task_id, "beta", reclaim_first=True)

        # Nothing was reclaimed, nothing was reassigned, no event was appended.
        assert _state(kanban_db, conn, task_id) == before


def test_request_review_validates_the_reviewer_profile(kanban_home):
    """``request_review(reviewer=...)`` reassigns the card, so it is an
    assignee mutation: handing a skills-carrying card to a reviewer profile
    that cannot load them just relocates the init crash to the review lane."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import KanbanSkillPreflightError

    _make_profile(kanban_home, "alpha", ["github-code-review"])
    _make_profile(kanban_home, "beta", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="card", assignee="alpha", skills=["github-code-review"],
        )
        run = kanban_db.claim_task(conn, task_id)
        before = _state(kanban_db, conn, task_id)

        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.request_review(
                conn, task_id, summary="done", reviewer="beta",
                expected_run_id=run.current_run_id,
            )
        assert excinfo.value.profile == "beta"
        assert excinfo.value.missing == ("github-code-review",)
        # The implementer keeps the card and its live claim.
        assert _state(kanban_db, conn, task_id) == before


def test_request_review_still_hands_off_to_a_reviewer_that_has_the_skill(kanban_home):
    """The check must not break the normal cross-profile review handoff."""
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "alpha", ["github-code-review"])
    _make_profile(kanban_home, "beta", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="card", assignee="alpha", skills=["github-code-review"],
        )
        run = kanban_db.claim_task(conn, task_id)
        assert kanban_db.request_review(
            conn, task_id, summary="done", reviewer="beta",
            expected_run_id=run.current_run_id,
        )
        task = kanban_db.get_task(conn, task_id)
        assert (task.status, task.assignee) == ("review", "beta")


def test_reassign_to_a_capable_profile_still_reclaims_and_moves_the_card(kanban_home):
    """The destructive path stays available when the target is valid."""
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "alpha", ["github-code-review"])
    _make_profile(kanban_home, "beta", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="card", assignee="alpha", skills=["github-code-review"],
        )
        assert kanban_db.claim_task(conn, task_id) is not None
        assert kanban_db.reassign_task(conn, task_id, "beta", reclaim_first=True) is True
        task = kanban_db.get_task(conn, task_id)
        assert (task.assignee, task.claim_lock) == ("beta", None)


def test_a_preflight_block_clears_initialization_only_failure_state(kanban_home):
    """A card that crash-looped on this exact misconfiguration before the
    preflight existed carries a nonzero failure streak. Those failures were
    never implementation failures, so once the card is refused for
    configuration they must not still count against it — otherwise the fixed
    card resumes one bad tick away from the auto-block breaker."""
    import json

    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    _make_profile(kanban_home, "claudecode", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(conn, title="legacy card", assignee="claudecode")
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET skills = ?, consecutive_failures = 2, "
                "last_failure_error = ? WHERE id = ?",
                (json.dumps(["still-missing"]),
                 "Unknown skill(s): still-missing", task_id),
            )

    with kanban_db_connect.connect_closing() as conn:
        kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        task = kanban_db.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_a_preflight_block_preserves_unrelated_implementation_failure_state(kanban_home):
    """A later configuration block must not erase real implementation history;
    only the worker-init ``Unknown skill(s): ...`` failure is stale."""
    import json

    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    _make_profile(kanban_home, "claudecode", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(conn, title="legacy card", assignee="claudecode")
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET skills = ?, consecutive_failures = 2, "
                "last_failure_error = ? WHERE id = ?",
                (json.dumps(["still-missing"]), "implementation boom", task_id),
            )

    with kanban_db_connect.connect_closing() as conn:
        kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        task = kanban_db.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 2
        assert task.last_failure_error == "implementation boom"


def test_the_corrected_card_resumes_with_no_stale_initialization_failures(kanban_home):
    """End of the incident: install the skill, unblock, and the card dispatches
    normally with a clean budget."""
    import json

    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    from tests.hermes_cli.test_kanban_skill_preflight import _write_skill

    profile_dir = _make_profile(kanban_home, "claudecode", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(conn, title="legacy card", assignee="claudecode")
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET skills = ?, consecutive_failures = 2, "
                "last_failure_error = ? WHERE id = ?",
                (json.dumps(["late-skill"]), "Unknown skill(s): late-skill", task_id),
            )
        kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)

    _write_skill(profile_dir / "skills", "late-skill")
    spawned = []
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.unblock_task(conn, task_id)
        kanban_db_dispatch.dispatch_once(
            conn, spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 4242)[1],
        )
        task = kanban_db.get_task(conn, task_id)
    assert spawned == [task_id]
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
