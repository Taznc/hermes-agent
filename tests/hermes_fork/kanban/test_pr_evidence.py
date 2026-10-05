"""Scoped PR evidence through the real dispatch/preclaim path; no live boards."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_fork.kanban.test_pr_requeue import PR, board, _events, _guarded  # noqa: F401


@pytest.mark.parametrize("author", ["human", "reviewer", "orchestrator", "dispatcher"])
def test_unrelated_authors_do_not_suppress_dispatch(board, author):
    conn, spawned, spawn = board
    tid = kb.create_task(conn, title="card", assignee="coder")
    kb.add_comment(conn, tid, author, f"Prior art: {PR}")
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) is None
    assert spawned == [tid], (result, kb.get_task(conn, tid).last_failure_error)
    assert _events(conn, tid, "active_pr_recovery") == []


@pytest.mark.parametrize("remote", ["https://github.com/Taznc/hermes-agent.git", "git@github.com:Taznc/hermes-agent.git", "ssh://git@github.com/Taznc/hermes-agent.git"])
def test_linked_project_scopes_worker_citations(board, tmp_path, remote):
    import subprocess
    from hermes_cli import projects_db as pdb
    conn, spawned, spawn = board
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid", "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    with pdb.connect_closing() as pconn:
        pid = pdb.create_project(pconn, name="target", primary_path=str(repo))
    tid = kb.create_task(conn, title="card", assignee="coder", project_id=pid, workspace_kind="scratch")
    kb.add_comment(conn, tid, "coder", "Prior art: https://github.com/Elsewhere/other/pull/1")
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) is None
    assert spawned == [tid], (result, kb.get_task(conn, tid).last_failure_error)


def _failed_pr(conn, monkeypatch, *, outcome="crashed", profile="coder", age=300, max_retries=3):
    now = 1_800_000_000
    monkeypatch.setattr(kbd.time, "time", lambda: now - age - 10)
    tid = kb.create_task(conn, title="failed PR", assignee=profile, max_retries=max_retries)
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, profile, f"Opened {PR}")
    monkeypatch.setattr(kbd.time, "time", lambda: now - age)
    kbd._record_task_failure(conn, tid, "worker ended", outcome=outcome,
                             release_claim=True, end_run=True)
    monkeypatch.setattr(kbd.time, "time", lambda: now)
    return tid


@pytest.mark.parametrize("outcome", ["crashed", "timed_out", "interrupted"])
def test_failed_worker_pr_retries_after_300_seconds(board, monkeypatch, outcome):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch, outcome=outcome)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) is None
    assert spawned == [tid], (result, kb.get_task(conn, tid).last_failure_error)
    (receipt,) = _events(conn, tid, "active_pr_recovery")
    assert receipt["recovery_reason"] == "failed_run"
    assert receipt["prior_outcome"] == outcome
    assert receipt["pr_urls"] == [PR]
    assert "do not create a duplicate PR" in kb.build_worker_context(conn, tid)


@pytest.mark.parametrize("age", [0, 299])
def test_failed_pr_cooldown_is_not_released_early(board, monkeypatch, age):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch, age=age)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert not spawned
    assert not _events(conn, tid, "active_pr_recovery")


@pytest.mark.parametrize("author,expected", [("coder", "active_pr"), ("human", None)])
def test_only_newer_matching_comment_rearms_failed_pr(board, monkeypatch, author, expected):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch)
    kb.add_comment(conn, tid, author, f"Update: {PR}")
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == expected
    assert bool(spawned) == (expected is None)


def test_failed_pr_preview_writes_nothing(board, monkeypatch):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch)
    changes = conn.total_changes
    result = kbd.dispatch_once(conn, spawn_fn=spawn, dry_run=True)
    assert [s[0] for s in result.spawned] == [tid]
    assert not spawned
    assert conn.total_changes == changes
    assert kb.get_task(conn, tid).status == "ready"


def test_recovery_does_not_reset_retry_cap_or_claim_owner(board, monkeypatch):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch, max_retries=2)
    kbd.dispatch_once(conn, spawn_fn=spawn)
    task = kb.get_task(conn, tid)
    assert task.assignee == "coder"
    assert task.consecutive_failures == 1
    run_id = task.current_run_id
    assert not kb.complete_task(conn, tid, expected_run_id=run_id - 1, summary="stale")
    assert kbd._record_task_failure(conn, tid, "again", outcome="crashed",
                                    release_claim=True, end_run=True)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert not result.spawned
    assert spawned == [tid]
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.get_task(conn, tid).consecutive_failures == 2


def test_other_profile_failed_run_cannot_authorize_recovery(board, monkeypatch):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch)
    monkeypatch.setattr(kbd.time, "time", lambda: 1_800_000_000 - 300)
    kb.assign_task(conn, tid, "other")
    kb.add_comment(conn, tid, "other", f"Taking over {PR}")
    monkeypatch.setattr(kbd.time, "time", lambda: 1_800_000_000)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert not spawned


@pytest.mark.parametrize("outcome", ["spawn_failed", "blocked"])
def test_other_outcomes_do_not_authorize_failed_pr_recovery(board, monkeypatch, outcome):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch, outcome=outcome)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert not spawned


def test_latest_ended_run_supersedes_older_failure(board, monkeypatch):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch)
    kb.claim_task(conn, tid)
    kbd._record_task_failure(conn, tid, "spawn", outcome="spawn_failed", release_claim=True, end_run=True)
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert not spawned


def test_unknown_scratch_repo_preserves_worker_evidence(board):
    conn, spawned, spawn = board
    tid = kb.create_task(conn, title="scratch", assignee="coder")
    kb.add_comment(conn, tid, "coder", f"Opened {PR}")
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert not spawned


def test_receipt_does_not_rearm_after_failed_recovery_spawn(board, monkeypatch):
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch, max_retries=5)
    def fail(task, workspace):
        raise RuntimeError("spawn failed")
    kbd.dispatch_once(conn, spawn_fn=fail)
    # spawn_failed is not T2 failure recovery, unlike T1 deliberate requeue.
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert len(_events(conn, tid, "active_pr_recovery")) == 1
    assert not spawned


def test_scoped_latest_comment_not_unrelated_handoff_controls_guard(board, monkeypatch):
    conn, spawned, spawn = board
    now = 1_800_000_000
    monkeypatch.setattr(kbd.time, "time", lambda: now - 300)
    tid = kb.create_task(conn, title="handoff", assignee="old")
    kb.add_comment(conn, tid, "human", f"See {PR}")
    monkeypatch.setattr(kbd.time, "time", lambda: now - 200)
    kb.assign_task(conn, tid, "coder")
    monkeypatch.setattr(kbd.time, "time", lambda: now - 100)
    kb.add_comment(conn, tid, "coder", f"Opened {PR}")
    # A newer human citation must not override the worker's genuine PR hold.
    monkeypatch.setattr(kbd.time, "time", lambda: now)
    kb.add_comment(conn, tid, "human", f"Prior art {PR}")
    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert _guarded(result, tid) == "active_pr"
    assert not spawned


def test_worktree_fallback_is_local_only_and_does_not_create_projects_db(board, tmp_path, monkeypatch):
    import socket
    import subprocess
    from hermes_cli import projects_db as pdb
    from hermes_fork.kanban import pr_evidence as evidence
    conn, _, _ = board
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Taznc/hermes-agent.git"], check=True)
    tid = kb.create_task(conn, title="worktree", assignee="coder", workspace_kind="worktree", workspace_path=str(repo))
    kb.add_comment(conn, tid, "coder", f"Opened {PR}; prior art https://github.com/Other/repo/pull/1")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET project_id = 'missing' WHERE id = ?", (tid,))
    original_run = subprocess.run
    calls = []
    def local_only(args, **kw):
        assert args == ["git", "-C", str(repo), "config", "--local", "--get", "remote.origin.url"]
        assert kw["timeout"] == 1
        calls.append(args)
        return original_run(args, **kw)
    def no_network(*a, **kw):
        pytest.fail("network under dispatch lock")
    monkeypatch.setattr(evidence.subprocess, "run", local_only)
    monkeypatch.setattr(socket, "socket", no_network)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    _, urls = evidence.evidence(conn, task, int(kbd.time.time()))
    assert urls == [PR]
    assert calls
    assert not pdb.projects_db_path().exists()
    assert evidence.release(conn, tid, "active_pr", dry_run=False) == "active_pr"


def test_recovery_receipt_ignores_unrelated_urls(board, monkeypatch):
    from hermes_fork.kanban import pr_evidence as evidence
    conn, spawned, spawn = board
    tid = _failed_pr(conn, monkeypatch)
    monkeypatch.setattr(evidence, "task_repo", lambda task: "taznc/hermes-agent")
    kb.add_comment(conn, tid, "coder", "See https://github.com/Other/repo/pull/1")
    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert spawned == [tid]
    (receipt,) = _events(conn, tid, "active_pr_recovery")
    assert receipt["pr_urls"] == [PR]
    assert "Other/repo" not in receipt["recovery"]


def test_missing_or_slow_local_remote_preserves_hold(board, monkeypatch):
    import subprocess
    from hermes_fork.kanban import pr_evidence as evidence
    conn, _, _ = board
    tid = kb.create_task(conn, title="slow repo", assignee="coder", workspace_kind="worktree", workspace_path="/missing")
    kb.add_comment(conn, tid, "coder", f"Opened {PR}")
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("git", 1)
    monkeypatch.setattr(evidence.subprocess, "run", timeout)
    assert evidence.release(conn, tid, "active_pr", dry_run=True) == "active_pr"


def test_t1_direct_contract_still_requires_deliberate_requeue(board, monkeypatch):
    from hermes_fork.kanban import pr_requeue
    conn, _, _ = board
    tid = _failed_pr(conn, monkeypatch)
    assert pr_requeue.release(conn, tid, "active_pr", dry_run=True) == "active_pr"
    assert not _events(conn, tid, "active_pr_recovery")


@pytest.mark.parametrize("guard", [None, "recent_success", "blocker_auth", "rate_limit_cooldown", "infrastructure_cooldown"])
def test_other_guard_reasons_pass_through(board, monkeypatch, guard):
    from hermes_fork.kanban import pr_evidence
    conn, _, _ = board
    tid = _failed_pr(conn, monkeypatch)
    assert pr_evidence.release(conn, tid, guard, dry_run=False) == guard
    assert not _events(conn, tid, "active_pr_recovery")
