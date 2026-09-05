"""End-to-end regressions for the Kanban review lifecycle.

These tests cover the two review models that must coexist:

* first-class same-card review, including an autonomous reviewer requesting
  changes and routing the task back to the original implementer; and
* legacy downstream review cards, where a sticky ``review-required`` parent
  can silently starve its reviewer child and therefore needs an immediate,
  graph-aware diagnostic.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _event(events, kind: str):
    return [event for event in events if event.kind == kind][-1]


def _run(runs, outcome: str):
    return [run for run in runs if run.outcome == outcome][-1]


def _claimed_review(
    conn,
    title: str,
    *,
    ttl_seconds: int | None = None,
    max_runtime_seconds: int | None = None,
):
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="builder",
        max_runtime_seconds=max_runtime_seconds,
    )
    implementation = kb.claim_task(conn, task_id, claimer="builder:test")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready for independent review",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(
        conn,
        task_id,
        ttl_seconds=ttl_seconds,
    )
    assert review is not None
    return task_id, review


def test_same_card_review_supports_changes_and_approval_without_block_loop(conn):
    task_id = kb.create_task(conn, title="Implement guarded export", assignee="builder")
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None

    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="Implementation and focused tests are ready.",
        metadata={"commit": "abc123"},
        expected_run_id=implementation.current_run_id,
    )

    awaiting_review = kb.get_task(conn, task_id)
    assert awaiting_review is not None
    assert awaiting_review.status == "review"
    assert awaiting_review.assignee == "reviewer"
    assert awaiting_review.current_run_id is None

    first_events = kb.list_events(conn, task_id)
    requested = _event(first_events, "review_requested")
    assert requested.payload["implementer"] == "builder"
    assert requested.payload["reviewer"] == "reviewer"
    assert requested.payload["summary"] == "Implementation and focused tests are ready."
    implementation_run = _run(kb.list_runs(conn, task_id), "review_requested")
    assert implementation_run.summary == "Implementation and focused tests are ready."
    assert implementation_run.metadata == {"commit": "abc123"}

    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason="Add a regression for the fallback branch.",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")

    rework = kb.get_task(conn, task_id)
    assert rework is not None
    assert rework.status == "ready"
    assert rework.assignee == "builder"
    assert rework.current_run_id is None
    changes = _event(kb.list_events(conn, task_id), "changes_requested")
    assert changes.payload is not None
    assert changes.payload["reason"] == "Add a regression for the fallback branch."
    assert changes.payload["implementer"] == "builder"
    assert changes.payload["reviewer"] == "reviewer"
    _run(kb.list_runs(conn, task_id), "changes_requested")

    implementation_2 = kb.claim_task(conn, task_id, claimer="builder:2")
    assert implementation_2 is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="Fallback regression added.",
        expected_run_id=implementation_2.current_run_id,
    )
    awaiting_rereview = kb.get_task(conn, task_id)
    assert awaiting_rereview is not None
    assert awaiting_rereview.status == "review"
    assert awaiting_rereview.assignee == "reviewer"
    review_2 = kb.claim_review_task(conn, task_id, claimer="reviewer:2")
    assert review_2 is not None
    assert review_2.assignee == "reviewer"
    review_run = kb.latest_run(conn, task_id)
    assert review_run is not None
    assert review_run.profile == "reviewer"
    assert kb.complete_task(
        conn,
        task_id,
        summary="Approved after independent verification.",
        expected_run_id=review_2.current_run_id,
    )

    completed = kb.get_task(conn, task_id)
    assert completed is not None
    assert completed.status == "done"
    assert completed.block_recurrences == 0


@pytest.mark.parametrize("bad_payload", [None, "{not-json", "{}"])
def test_rereview_requires_explicit_reviewer_when_provenance_is_invalid(
    conn,
    bad_payload: str | None,
) -> None:
    task_id, review = _claimed_review(conn, "Malformed reviewer provenance")
    assert kb.request_changes(
        conn,
        task_id,
        reason="Correct the implementation.",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    with kb.write_txn(conn):
        if bad_payload is None:
            conn.execute(
                "DELETE FROM task_events "
                "WHERE task_id = ? AND kind = 'changes_requested'",
                (task_id,),
            )
        else:
            conn.execute(
                "UPDATE task_events SET payload = ? "
                "WHERE id = (SELECT id FROM task_events "
                "WHERE task_id = ? AND kind = 'changes_requested' "
                "ORDER BY id DESC LIMIT 1)",
                (bad_payload, task_id),
            )

    implementation = kb.claim_task(conn, task_id, claimer="builder:retry")
    assert implementation is not None
    assert not kb.request_review(
        conn,
        task_id,
        summary="Corrected implementation.",
        expected_run_id=implementation.current_run_id,
    )
    unchanged = kb.get_task(conn, task_id)
    assert unchanged is not None
    assert unchanged.status == "running"
    assert unchanged.assignee == "builder"

    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="Corrected implementation.",
        expected_run_id=implementation.current_run_id,
    )
    restored = kb.get_task(conn, task_id)
    assert restored is not None
    assert restored.status == "review"
    assert restored.assignee == "reviewer"


def test_review_changes_reapply_parent_gate(conn):
    parent_id = kb.create_task(conn, title="Upstream prerequisite", assignee="planner")
    task_id = kb.create_task(
        conn,
        title="Dependent implementation",
        assignee="builder",
        parents=[parent_id],
    )

    # Move the task through review while its parent is temporarily terminal,
    # then make the parent non-terminal again before changes are requested.
    assert kb.complete_task(conn, parent_id)
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="Ready for review.",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent_id,))
    conn.commit()

    assert kb.request_changes(
        conn,
        task_id,
        reason="Parent contract changed; rework after it lands.",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    regated = kb.get_task(conn, task_id)
    assert regated is not None
    assert regated.status == "todo"


def test_parent_reopen_blocks_request_review_until_parent_is_done(conn) -> None:
    parent_id = kb.create_task(conn, title="Parent", assignee="planner")
    assert kb.complete_task(conn, parent_id)
    task_id = kb.create_task(
        conn,
        title="Implementation with reopened parent",
        assignee="builder",
        parents=[parent_id],
    )
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent_id,))
    assert not kb.request_review(
        conn,
        task_id,
        summary="must wait",
        expected_run_id=implementation.current_run_id,
    )
    still_running = kb.get_task(conn, task_id)
    assert still_running is not None
    assert still_running.status == "running"
    assert kb.complete_task(conn, parent_id)
    assert kb.request_review(
        conn,
        task_id,
        summary="parent stable",
        expected_run_id=implementation.current_run_id,
    )


@pytest.mark.parametrize("bad_payload", ["{not-json", "[]"])
def test_request_changes_fails_closed_on_malformed_review_provenance(
    conn,
    bad_payload: str,
):
    task_id = kb.create_task(conn, title="Malformed handoff", assignee="builder")
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="Ready.",
        expected_run_id=implementation.current_run_id,
    )
    conn.execute(
        "UPDATE task_events SET payload = ? "
        "WHERE task_id = ? AND kind = 'review_requested'",
        (bad_payload, task_id),
    )
    conn.commit()
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None

    ok, detail = kb.request_changes(
        conn,
        task_id,
        reason="Needs changes.",
        expected_run_id=review.current_run_id,
    )
    assert ok is False
    assert "implementer provenance" in (detail or "")
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert task.assignee == "reviewer"
    assert task.current_run_id == review.current_run_id


def test_reclaim_fails_safe_on_non_object_claim_provenance(conn) -> None:
    task_id, _review = _claimed_review(conn, "Non-object claimed payload")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET payload = '[]' "
            "WHERE task_id = ? AND kind = 'claimed' "
            "AND run_id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (task_id, task_id),
        )
    assert kb.reclaim_task(conn, task_id, signal_fn=lambda *_args: None)
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"


@pytest.mark.parametrize(
    "reclaim_kind",
    ["spawn_failure", "expired_claim", "manual_reclaim", "stale_heartbeat"],
)
def test_interrupted_review_runs_retry_in_review_phase(
    conn,
    reclaim_kind: str,
) -> None:
    task_id, review = _claimed_review(
        conn,
        f"Retry review after {reclaim_kind}",
        ttl_seconds=-1 if reclaim_kind == "expired_claim" else None,
    )

    if reclaim_kind == "spawn_failure":
        assert not kbd._record_task_failure(
            conn,
            task_id,
            "reviewer process failed to spawn",
            outcome="spawn_failed",
            failure_limit=3,
            release_claim=True,
            end_run=True,
        )
    elif reclaim_kind == "expired_claim":
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, task_id),
            )
        assert kb.release_stale_claims(conn) == 1
    elif reclaim_kind == "manual_reclaim":
        assert kb.reclaim_task(conn, task_id, reason="operator retry")
    else:
        old = int(time.time()) - 1_000
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
                "WHERE id = ?",
                (old, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, review.current_run_id),
            )
        assert kbd.detect_stale_running(conn, stale_timeout_seconds=1) == [task_id]

    retried = kb.get_task(conn, task_id)
    assert retried is not None
    assert retried.status == "review"
    assert retried.current_run_id is None
    event = kb.list_events(conn, task_id=task_id)[-1]
    assert event.payload is not None
    assert event.payload.get("retry_status") == "review"


def test_review_retry_still_trips_the_failure_breaker(conn) -> None:
    task_id, _review = _claimed_review(conn, "Reviewer repeatedly fails")
    assert kbd._record_task_failure(
        conn,
        task_id,
        "reviewer cannot start",
        outcome="spawn_failed",
        failure_limit=1,
        release_claim=True,
        end_run=True,
    )
    blocked = kb.get_task(conn, task_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    gave_up = _event(kb.list_events(conn, task_id), "gave_up")
    assert gave_up.payload is not None
    assert gave_up.payload["retry_status"] == "review"
    assert kb.unblock_task(conn, task_id)
    unblocked = kb.get_task(conn, task_id)
    assert unblocked is not None
    assert unblocked.status == "review"


def test_review_escalation_unblocks_back_to_review(conn) -> None:
    task_id, review = _claimed_review(conn, "External review escalation")
    assert kb.block_task(
        conn,
        task_id,
        reason="needs_input: maintainer decision required",
        kind="needs_input",
        expected_run_id=review.current_run_id,
    )
    blocked_event = _event(kb.list_events(conn, task_id), "blocked")
    assert blocked_event.payload is not None
    assert blocked_event.payload["source_status"] == "review"
    assert kb.unblock_task(conn, task_id)
    resumed = kb.get_task(conn, task_id)
    assert resumed is not None
    assert resumed.status == "review"


def test_review_dependency_wait_reenters_review_after_parent_finishes(conn) -> None:
    parent_id = kb.create_task(conn, title="Parent", assignee="planner")
    assert kb.complete_task(conn, parent_id)
    task_id = kb.create_task(
        conn,
        title="Review after dependency refresh",
        assignee="builder",
        parents=[parent_id],
    )
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent_id,))
    assert kb.block_task(
        conn,
        task_id,
        reason="dependency: parent contract is being refreshed",
        kind="dependency",
        expected_run_id=review.current_run_id,
    )
    waiting = kb.get_task(conn, task_id)
    assert waiting is not None
    assert waiting.status == "todo"
    assert kb.complete_task(conn, parent_id)
    resumed = kb.get_task(conn, task_id)
    assert resumed is not None
    assert resumed.status == "review"


def test_crashed_and_timed_out_review_runs_retry_in_review_phase(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid, _worker_unit=None: ("nonzero_exit", 1))
    old = int(time.time()) - 1_000

    timed_out_id, timed_out_run = _claimed_review(
        conn,
        "Timeout during review",
        max_runtime_seconds=1,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
            (999_998, old, timed_out_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, started_at = ? WHERE id = ?",
            (999_998, old, timed_out_run.current_run_id),
        )
    assert timed_out_id in kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None)
    timed_out = kb.get_task(conn, timed_out_id)
    assert timed_out is not None
    assert timed_out.status == "review"

    crashed_id, crashed_run = _claimed_review(conn, "Crash during review")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
            (999_999, old, crashed_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, started_at = ? WHERE id = ?",
            (999_999, old, crashed_run.current_run_id),
        )
    assert crashed_id in kbd.detect_crashed_workers(conn)
    crashed = kb.get_task(conn, crashed_id)
    assert crashed is not None
    assert crashed.status == "review"


def test_goal_run_status_is_bound_to_original_run(conn) -> None:
    task_id = kb.create_task(conn, title="Goal handoff race", assignee="builder")
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    assert kb.goal_run_status(
        conn, task_id, implementation.current_run_id
    ) == "review"

    assert kb.request_changes(
        conn,
        task_id,
        reason="fix it",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    successor = kb.claim_task(conn, task_id)
    assert successor is not None
    assert kb.goal_run_status(
        conn, task_id, review.current_run_id
    ) == "changes_requested"
    assert kb.goal_run_status(
        conn, task_id, successor.current_run_id
    ) == "running"
    assert not kb.block_task(
        conn,
        task_id,
        reason="stale reviewer must not block successor",
        expected_run_id=review.current_run_id,
    )
    current = kb.get_task(conn, task_id)
    assert current is not None
    assert current.status == "running"
    assert current.current_run_id == successor.current_run_id


def test_parked_review_approval_without_evidence_still_creates_audit_run(conn) -> None:
    task_id = kb.create_task(conn, title="Manual approval", assignee="reviewer")
    assert kb.request_review(conn, task_id, summary="implementation handoff")
    assert kb.complete_task(conn, task_id)
    completed_event = _event(kb.list_events(conn, task_id), "completed")
    assert completed_event.run_id is not None
    run = kb.latest_run(conn, task_id)
    assert run is not None
    assert run.id == completed_event.run_id
    assert run.outcome == "completed"
    assert run.profile == "reviewer"
    assert run.summary == "Review approved without additional evidence."
    assert run.metadata == {
        "source_status": "review",
        "approval": "manual",
    }


def test_legacy_review_child_deadlock_is_reported_immediately(conn):
    implementation_id = kb.create_task(
        conn,
        title="Implement export",
        assignee="builder",
    )
    reviewer_id = kb.create_task(
        conn,
        title="Review export",
        assignee="reviewer",
        parents=[implementation_id],
    )
    implementation = kb.claim_task(conn, implementation_id, claimer="builder:1")
    assert implementation is not None
    assert kb.block_task(
        conn,
        implementation_id,
        reason="review-required: implementation ready for independent review",
        expected_run_id=implementation.current_run_id,
    )
    reviewer_task = kb.get_task(conn, reviewer_id)
    assert reviewer_task is not None
    assert reviewer_task.status == "todo"
    assert kb.recompute_ready(conn) == 0

    task = kb.get_task(conn, implementation_id)
    diagnostics = kd.compute_task_diagnostics(
        task,
        kb.list_events(conn, implementation_id),
        kb.list_runs(conn, implementation_id),
        graph={
            "children": [
                {
                    "id": reviewer_id,
                    "title": "Review export",
                    "status": "todo",
                }
            ]
        },
    )

    deadlocks = [d for d in diagnostics if d.kind == "review_dependency_deadlock"]
    assert len(deadlocks) == 1
    deadlock = deadlocks[0]
    assert deadlock.severity == "error"
    assert deadlock.data["blocked_parent_id"] == implementation_id
    assert deadlock.data["waiting_child_ids"] == [reviewer_id]
    assert any(action.kind == "cli_hint" for action in deadlock.actions)


def test_hard_block_with_waiting_child_is_not_mislabeled_as_review_deadlock(conn):
    implementation_id = kb.create_task(
        conn, title="Implement export", assignee="builder"
    )
    child_id = kb.create_task(
        conn,
        title="Publish export",
        assignee="release",
        parents=[implementation_id],
    )
    implementation = kb.claim_task(conn, implementation_id, claimer="builder:1")
    assert implementation is not None
    assert kb.block_task(
        conn,
        implementation_id,
        reason="needs_input: production credentials unavailable",
        expected_run_id=implementation.current_run_id,
    )

    diagnostics = kd.compute_task_diagnostics(
        kb.get_task(conn, implementation_id),
        kb.list_events(conn, implementation_id),
        kb.list_runs(conn, implementation_id),
        graph={
            "children": [{"id": child_id, "title": "Publish export", "status": "todo"}]
        },
    )
    assert not any(d.kind == "review_dependency_deadlock" for d in diagnostics)


def _failures(conn, task_id: str) -> int:
    return int(conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0])


def test_review_transitions_preserve_consecutive_failures(conn) -> None:
    """M2 regression: review transitions neither reset nor increment the
    circuit-breaker counter.

    A task with consecutive_failures=1 that cycles through
    request_review -> request_changes -> re-request keeps the counter at 1;
    a crash after request_changes increments it to 2 and trips a
    failure_limit=2 breaker. Only complete_task's success path resets it.
    """
    task_id = kb.create_task(conn, title="flaky feature", assignee="builder")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
            (task_id,),
        )

    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn, task_id, summary="v1", reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    assert _failures(conn, task_id) == 1  # request_review preserved it

    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    assert kb.request_changes(
        conn, task_id, reason="needs fixes",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    assert _failures(conn, task_id) == 1  # request_changes preserved it

    retry = kb.claim_task(conn, task_id, claimer="builder:2")
    assert retry is not None
    assert kb.request_review(
        conn, task_id, summary="v2",
        expected_run_id=retry.current_run_id,
    )
    assert _failures(conn, task_id) == 1  # full re-review cycle: still 1

    # reopen_review_task (manual changes-requested) also preserves it.
    assert kb.reopen_review_task(conn, task_id)
    assert _failures(conn, task_id) == 1

    # A crash now increments 1 -> 2 and trips a failure_limit=2 breaker —
    # the counter accumulated across the review cycle instead of being
    # amnesia-reset back to 0.
    tripped = kbd._record_task_failure(
        conn, task_id, "worker crashed", outcome="crashed", failure_limit=2,
    )
    assert tripped is True
    assert _failures(conn, task_id) == 2
    assert kb.get_task(conn, task_id).status == "blocked"

    # Sanity: complete_task's success path still clears the counter.
    ok_id = kb.create_task(conn, title="healthy", assignee="builder")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
            (ok_id,),
        )
    assert kb.complete_task(conn, ok_id, summary="done")
    assert _failures(conn, ok_id) == 0


# ---------------------------------------------------------------------------
# Review no-verdict dispatch loop + circuit-breaker promotion mismatch
# (parent t_00fea4fd root cause; this card's fix).
# ---------------------------------------------------------------------------


def _drive_worker_exit(conn, tid, fake_pid, raw_status):
    """Claim ``tid``'s current running run against ``fake_pid`` and reap it.

    Mirrors ``test_kanban_core_functionality._drive_worker_exit`` but does
    NOT re-claim: the caller is expected to already have a live claim (e.g.
    from ``claim_review_task``) that this function kills off. ``started_at``
    is backdated past the crash-detection grace period so the reaper does
    not skip a freshly-claimed run as still-launching.
    """
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    host_prefix = _kb._claimer_id().split(":", 1)[0]
    task = _kb.get_task(conn, tid)
    assert task is not None and task.status == "running"
    old = int(time.time()) - 1_000
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, claim_lock = ?, started_at = ? "
            "WHERE id = ?",
            (fake_pid, f"{host_prefix}:mock", old, tid),
        )
        if task.current_run_id:
            conn.execute(
                "UPDATE task_runs SET worker_pid = ?, started_at = ? "
                "WHERE id = ?",
                (fake_pid, old, task.current_run_id),
            )
    _kbd._record_worker_exit(fake_pid, raw_status)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kbd.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def test_review_claim_no_verdict_produces_neutral_audit_and_no_reclaim(conn) -> None:
    """1. review claim -> no verdict -> no second claim.

    A claimed reviewer whose process exits cleanly (rc=0) without calling
    kanban_complete / kanban_request_changes / kanban_block must land a
    ``review_no_verdict`` event (not ``protocol_violation``, not
    ``crashed``), must NOT increment ``consecutive_failures``, and the
    task must be un-claimable by a fresh dispatcher tick until an
    explicit ``kanban_unblock``.
    """
    task_id, review = _claimed_review(conn, "Idle reviewer pass")
    before_failures = kb.get_task(conn, task_id).consecutive_failures

    crashed = _drive_worker_exit(conn, task_id, 881001, 0)  # rc=0 clean exit
    assert crashed == []  # not reported as a crash

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.consecutive_failures == before_failures

    events = kb.list_events(conn, task_id)
    kinds = [e.kind for e in events]
    assert "review_no_verdict" in kinds
    assert "protocol_violation" not in kinds
    assert "crashed" not in kinds
    assert "gave_up" not in kinds

    verdict_event = _event(events, "review_no_verdict")
    assert verdict_event.payload is not None
    assert verdict_event.payload.get("retry_status") == "review"

    run = _run(kb.list_runs(conn, task_id), "review_no_verdict")
    assert run.status == "review_no_verdict"

    # A dispatcher tick must NOT re-promote/re-claim it — sticky like a
    # worker-initiated kanban_block.
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, task_id).status == "blocked"
    assert kb.claim_review_task(conn, task_id) is None

    # Explicit requeue reopens it for another reviewer pass.
    assert kb.unblock_task(conn, task_id)
    reopened = kb.get_task(conn, task_id)
    assert reopened is not None
    assert reopened.status == "review"
    second_pass = kb.claim_review_task(conn, task_id, claimer="reviewer:2")
    assert second_pass is not None


def test_repeated_unblock_review_cycles_never_trip_block_loop_accounting(
    conn,
) -> None:
    """2. repeated unblock -> review cycles never trip block-loop accounting.

    ``review_no_verdict`` is a neutral audit outcome, not a worker/operator
    ``kanban_block`` — cycling through several no-verdict parks and
    unblocks must never touch ``block_recurrences`` or route the card to
    ``triage`` the way a same-cause re-block would.
    """
    task_id, _review = _claimed_review(conn, "Repeatedly idle reviewer")
    for i in range(5):
        crashed = _drive_worker_exit(conn, task_id, 882000 + i, 0)
        assert crashed == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_recurrences == 0
        assert kb.unblock_task(conn, task_id)
        resumed = kb.get_task(conn, task_id)
        assert resumed is not None
        assert resumed.status == "review"
        claimed = kb.claim_review_task(conn, task_id, claimer=f"reviewer:{i}")
        assert claimed is not None

    final = kb.get_task(conn, task_id)
    assert final is not None
    assert final.block_recurrences == 0
    assert final.status != "triage"
    events = kb.list_events(conn, task_id)
    assert not any(e.kind == "block_loop_detected" for e in events)


def test_review_source_protocol_streak_trip_never_promotes_via_recompute_ready(
    conn,
) -> None:
    """3. review-source protocol streak trips -> recompute_ready never
    writes ``promoted {status: review}``.

    Reproduces the live ``t_023d0c6a`` bug: a review-claimed run whose
    worker exits cleanly WHILE the task is still ``running`` (i.e. NOT a
    reviewer no-verdict park — the review-source protocol-violation path)
    trips the force-tripped breaker after
    ``_PROTOCOL_VIOLATION_FAILURE_LIMIT`` consecutive violations. Before
    this fix, ``recompute_ready`` independently re-derived a higher
    ``effective_limit`` (default/dispatcher), saw the smaller unified
    ``consecutive_failures`` sitting under it, and wrongly emitted
    ``promoted {status: review}`` — reopening the review claim without
    an operator ever unblocking it.
    """
    task_id, review = _claimed_review(conn, "Review-source protocol streak")
    # Simulate the shape detect_crashed_workers produces for a review-source
    # protocol-violation streak: the main reclaim txn has ALREADY flipped
    # the task to its source phase (``review``, from the claimed event's
    # ``source_status``) before the post-txn accounting loop calls
    # ``_record_task_failure(release_claim=False, ...)`` — the same
    # call shape the streak's force-trip branch uses in
    # ``detect_crashed_workers``.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'review', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ?",
            (task_id,),
        )
    from hermes_cli import kanban_db_dispatch as kbd
    limit = kbd._PROTOCOL_VIOLATION_FAILURE_LIMIT
    for i in range(limit):
        tripped = kbd._record_task_failure(
            conn,
            task_id,
            error="worker exited cleanly without a terminal call",
            outcome="crashed",
            failure_limit=limit,
            force_trip=(i == limit - 1),
            release_claim=False,
            end_run=False,
            event_payload_extra={"protocol_violations": i + 1},
        )
        if i < limit - 1:
            assert tripped is False
        else:
            assert tripped is True

    blocked = kb.get_task(conn, task_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    # The unified counter is far under a generous dispatcher-configured
    # limit — exactly the mismatch that used to fool recompute_ready.
    generous_limit = blocked.consecutive_failures + 10
    assert kb.recompute_ready(conn, failure_limit=generous_limit) == 0
    still_blocked = kb.get_task(conn, task_id)
    assert still_blocked is not None
    assert still_blocked.status == "blocked"
    events = kb.list_events(conn, task_id)
    assert not any(
        e.kind == "promoted" and (e.payload or {}).get("status") == "review"
        for e in events
    )

    # Only an explicit unblock recovers it.
    assert kb.unblock_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == "review"
