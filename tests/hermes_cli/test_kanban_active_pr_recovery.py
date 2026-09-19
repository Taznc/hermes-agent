"""PR evidence must prevent duplicate work, not suppress failed-run recovery."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles


PR = "https://github.com/example/repo/pull/13"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(name)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert kb.kanban_db_path().is_relative_to(home)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    clock = [int(time.time())]
    monkeypatch.setattr(kbd.time, "time", lambda: clock[0])
    with kbc.connect() as conn:
        yield conn, clock


@pytest.mark.parametrize("exit_kind,exit_code,outcome", [
    ("clean_exit", 0, "crashed"),
    ("nonzero_exit", 1, "crashed"),
    ("signaled", signal.SIGTERM, "interrupted"),
    ("timeout", None, "timed_out"),
])
def test_failed_pr_run_recovers_after_cooldown_without_duplicate_or_retry_loop(
    sandbox, monkeypatch, caplog, exit_kind, exit_code, outcome,
):
    caplog.set_level("DEBUG", logger="hermes_cli.kanban_db")
    conn, clock = sandbox
    spawned = []
    dead = set()

    def spawn(task, workspace):
        pid = 910000 + len(spawned)
        spawned.append((task.id, pid))
        return pid

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid not in dead)
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: (exit_kind, exit_code))
    monkeypatch.setattr(kb, "_resolve_max_infra_interruptions", lambda: 1)
    monkeypatch.setattr(kbd, "_terminate_reclaimed_worker", lambda *a, **kw: {
        "termination_attempted": True, "host_local": True, "terminated": True,
    })
    tid = kb.create_task(
        conn, title="Recover publication handoff", assignee="builder", max_retries=2,
        max_runtime_seconds=60 if outcome == "timed_out" else None,
    )

    def end_worker():
        clock[0] += max(61, kb._resolve_crash_grace_seconds() + 1)
        dead.add(spawned[-1][1])
        if outcome == "timed_out":
            assert kbd.enforce_max_runtime(conn) == [tid]

    kbd.dispatch_once(conn, spawn_fn=spawn)
    task = kb.get_task(conn, tid)
    assert task is not None
    first_run = task.current_run_id
    kb.add_comment(conn, tid, author="builder", body=f"Verified work already published: {PR}")
    end_worker()
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert (tid, "active_pr") in result.respawn_guarded
    run = kb.latest_run(conn, tid)
    assert run is not None and run.outcome == outcome
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "ready"

    clock[0] += 299
    assert (tid, "active_pr") in kbd.dispatch_once(conn, spawn_fn=spawn).respawn_guarded
    clock[0] += 1
    # Dry-run predicts recovery but cannot consume it or append a receipt.
    assert [row[0] for row in kbd.dispatch_once(conn, dry_run=True).spawned] == [tid]
    assert not [e for e in kb.list_events(conn, tid) if e.kind == "active_pr_recovery"]
    recovered = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert [row[0] for row in recovered.spawned] == [tid]
    assert len(spawned) == 2
    receipt = [e for e in kb.list_events(conn, tid) if e.kind == "active_pr_recovery"]
    assert len(receipt) == 1
    task = kb.get_task(conn, tid)
    assert task is not None and receipt[0].run_id == task.current_run_id
    assert receipt[0].payload is not None
    assert receipt[0].payload["prior_run_id"] == first_run
    assert receipt[0].payload["pr_urls"] == [PR]
    assert "existing PR" in receipt[0].payload["recovery"]
    from hermes_cli.kanban_db_packet import build_worker_task_packet

    packet = build_worker_task_packet(conn, tid).to_dict()
    recovery_note = packet["history"]["preview"]["comments"][-1]
    assert recovery_note["author"] == "dispatcher"
    assert "Resume the existing PR" in recovery_note["body"]
    assert PR in recovery_note["body"]
    assert f"active_pr deferred task={tid}" in caplog.text
    assert f"active_pr recovery claimed task={tid}" in caplog.text
    deferred = [e for e in kb.list_events(conn, tid) if e.kind == "respawn_guarded"]
    assert deferred[-1].payload is not None
    assert deferred[-1].payload["eligible_at"] == clock[0]
    assert not kbd.dispatch_once(conn, spawn_fn=spawn).spawned

    # The existing failure/protocol breaker bounds even repeated recovery failures.
    end_worker()
    kbd.dispatch_once(conn, spawn_fn=spawn)
    clock[0] += 301
    assert not kbd.dispatch_once(conn, spawn_fn=spawn).spawned
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "blocked"
    assert len(spawned) == 2


def test_explicit_review_rework_releases_old_evidence_not_new_comments_or_other_guards(sandbox):
    conn, clock = sandbox
    tid = kb.create_task(conn, title="Revise existing PR", assignee="builder")
    implementation = kb.claim_task(conn, tid)
    assert implementation is not None
    kb.add_comment(conn, tid, author="builder", body=PR)
    assert kb.request_review(
        conn, tid, reviewer="reviewer", summary="Review existing PR",
        expected_run_id=implementation.current_run_id,
    )
    # Same-second run endings must pick the newer reviewer verdict, not the handoff.
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    assert kb.request_changes(
        conn, tid, reason="Fix failing check on the existing PR",
        expected_run_id=review.current_run_id,
        blockers=[{"basis": "original_ac", "reference": "test acceptance contract"}],
    ) == (True, "builder")
    assert kbd.check_respawn_guard(conn, tid) is None
    recovery = {}
    assert kbd.check_respawn_guard(conn, tid, pr_recovery=recovery) is None
    assert recovery["prior_run_id"] == review.current_run_id
    assert recovery["recovery_reason"] == "changes_requested"

    control = kb.create_task(conn, title="Only duplicate-work evidence", assignee="builder")
    kb.add_comment(conn, control, author="builder", body=PR)
    assert kbd.check_respawn_guard(conn, control) == "active_pr"
    assert kbd.check_respawn_guard(conn, control, lane="review") is None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error='authentication failed' WHERE id=?", (tid,))
    assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error=NULL WHERE id=?", (tid,))
    clock[0] += 1
    kb.add_comment(conn, tid, author="builder", body=f"New publication evidence: {PR}")
    recovery = {}
    assert kbd.check_respawn_guard(conn, tid, pr_recovery=recovery) == "active_pr"
    assert not recovery, "a newer comment must invalidate the old recovery receipt"
    clock[0] += kbd._RESPAWN_GUARD_PR_WINDOW + 1
    assert kbd.check_respawn_guard(conn, tid) is None
