"""Tests for the Kanban tool surface (tools/kanban_tools.py).

Verifies:
  - Tools are gated on HERMES_KANBAN_TASK: a normal chat session sees
    zero kanban tools in its schema; a worker session sees the kanban set.
  - Each handler's happy path.
  - Error paths (missing required args, bad metadata type, etc).
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_kanban_tools_hidden_without_env_var(monkeypatch, tmp_path):
    """Normal `hermes chat` sessions (no HERMES_KANBAN_TASK) must have
    zero kanban_* tools in their schema."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert kanban == set(), (
        f"kanban tools leaked into normal chat schema: {kanban}"
    )


# ---------------------------------------------------------------------------
# Handler happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_show_defaults_to_env_task_id(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_show({})
    d = json.loads(out)
    assert "task" in d
    assert d["task"]["id"] == worker_env
    assert d["task"]["status"] == "running"
    assert "worker_context" in d
    assert "runs" in d


def test_list_filters_tasks(monkeypatch, worker_env):
    """kanban_list gives orchestrators filtered board discovery."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        a = kb.create_task(conn, title="alpha", assignee="factory", priority=5)
        b = kb.create_task(conn, title="beta", assignee="reviewer")
        c = kb.create_task(conn, title="gamma", assignee="factory", tenant="other")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({"assignee": "factory", "status": "ready", "limit": 10})
    d = json.loads(out)
    ids = [t["id"] for t in d["tasks"]]
    assert ids == [a, c]
    assert d["count"] == 2
    assert d["tasks"][0]["title"] == "alpha"
    assert d["tasks"][0]["parent_count"] == 0
    assert b not in ids

    tenant_out = kt._handle_list({
        "assignee": "factory",
        "status": "ready",
        "tenant": "other",
    })
    tenant_ids = [t["id"] for t in json.loads(tenant_out)["tasks"]]
    assert tenant_ids == [c]


def test_complete_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "got the thing done",
        "metadata": {"files": 2},
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env
    # Verify via kernel
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.summary == "got the thing done"
        assert run.metadata == {"files": 2}
    finally:
        conn.close()


def test_complete_retry_with_empty_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with
    created_cards=[] (the documented escape hatch) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    # Hit the gate first.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": ["t_phantomdeadbeef"],
    }))
    assert rejected.get("error")

    # Retry with the escape hatch.
    ok = json.loads(kt._handle_complete({
        "summary": "retry without claims",
        "created_cards": [],
    }))
    assert ok.get("ok") is True

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_complete_orphaned_worker_gets_distinguishable_exit_signal(worker_env):
    """When this worker's own board row is deleted out from under it
    (t_749b0510's exact incident — delete_task on a live 'running' row),
    kanban_complete must return a distinguishable ``orphaned: true`` field
    instead of the same generic "unknown id or already terminal" error a
    plain typo would produce. That is the actionable clean-exit signal
    requested by t_963c89a2 item 3.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    try:
        # Manufacture the orphan state with a raw DELETE rather than
        # kb.delete_task: t_749b0510's guard now (correctly) REFUSES to delete a
        # 'running' row with a live worker, which is the very incident this
        # contract exists for. The guard closes one route into the state; it does
        # not make the state unreachable (gc/archive paths, direct DB surgery,
        # and any row deleted before the guard shipped all still produce it), so
        # the orphan-exit signal must still hold. Asserting through delete_task
        # here would test the guard, not this contract.
        with kb.write_txn(conn):
            conn.execute("DELETE FROM tasks WHERE id = ?", (worker_env,))
        assert kb.get_task(conn, worker_env) is None
    finally:
        conn.close()

    out = json.loads(kt._handle_complete({"summary": "trying to land after being orphaned"}))
    assert out.get("orphaned") is True, out
    assert out.get("task_id") == worker_env
    assert out.get("error")


def test_complete_bogus_task_id_is_not_reported_as_orphaned(monkeypatch, worker_env):
    """A plain wrong/hallucinated id (never a real row) must NOT get the
    orphan signal — only a task that this worker was actually scoped to via
    HERMES_KANBAN_TASK and that is now provably gone counts as orphaned."""
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_TASK", worker_env)
    out = json.loads(kt._handle_complete({
        "task_id": "t_neverexisted0",
        "summary": "should not be treated as an orphan",
    }))
    assert out.get("error")
    assert "orphaned" not in out


def test_heartbeat_orphaned_worker_gets_distinguishable_exit_signal(worker_env):
    """Same orphan-exit contract for kanban_heartbeat: today's fleet incident
    (t_749b0510's comment thread) showed a heartbeat on a deleted task
    returning a silent False with nothing actionable — this must now be a
    structured ``orphaned: true`` the worker can act on to stop."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    try:
        # Raw DELETE for the same reason as the kanban_complete case above:
        # t_749b0510's guard correctly refuses delete_task on a live running row.
        with kb.write_txn(conn):
            conn.execute("DELETE FROM tasks WHERE id = ?", (worker_env,))
        assert kb.get_task(conn, worker_env) is None
    finally:
        conn.close()

    out = json.loads(kt._handle_heartbeat({"note": "still alive?"}))
    assert out.get("orphaned") is True, out
    assert out.get("task_id") == worker_env


def test_complete_goal_mode_rejected_by_judge(monkeypatch, tmp_path):
    """Goal-mode tasks must pass the auxiliary judge before completion.
    Regression for #38367: workers bypassing the judge via early kanban_complete."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    # Set up isolated HERMES_HOME
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # Mock the judge to reject the completion. The gate only runs when a
    # judge is reachable, so force the availability probe True as well.
    def mock_judge_goal(goal, last_response, *, timeout=30.0, subgoals=None):
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        return "continue", "missing verification evidence", False, None, False

    monkeypatch.setattr("tools.kanban_tools.judge_goal", mock_judge_goal)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: True)

    # Attempt to complete should be rejected
    out = kt._handle_complete({"summary": "I did some stuff but not X"})
    d = json.loads(out)
    assert "error" in d
    assert "Goal completion rejected by judge" in d["error"]
    assert "missing verification evidence" in d["error"]
    assert f"parents=[{goal_task_id}]" in d["error"]

    # Verify the task is NOT completed in the DB
    conn2 = kbc.connect()
    try:
        task = kb.get_task(conn2, goal_task_id)
        assert task.status == "running"  # Should still be running, not done
    finally:
        conn2.close()


def test_block_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    d = json.loads(out)
    assert d["ok"] is True
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_block_rejects_wall_of_text_reason(worker_env):
    """A block reason is a board card, not a log file: prose beyond the cap is
    rejected with guidance to move diagnosis into kanban_comment."""
    from tools import kanban_tools as kt
    wall = "Deployment detail sentence. " * 60  # far past the prose cap
    d = json.loads(kt._handle_block({"reason": wall}))
    assert "error" in d
    assert "kanban_comment" in d["error"]
    # The task must NOT have been blocked.
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, worker_env).status != "blocked"
    finally:
        conn.close()


def test_block_reason_fences_do_not_count_toward_prose_cap(worker_env):
    """```cmd / ```choices fences are the structured payloads the UI wants —
    a long command or option set must never trip the brevity gate."""
    from tools import kanban_tools as kt
    reason = (
        "Restart the service to unblock me.\n"
        "```cmd\n" + ("x" * 900) + "\n```"
    )
    d = json.loads(kt._handle_block({"reason": reason}))
    assert d.get("ok") is True


def _make_goal_mode_worker_env(monkeypatch, tmp_path):
    """Set up an isolated HERMES_HOME with one claimed goal_mode task,
    matching the pattern used by the kanban_complete judge gate tests."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-block-test", assignee="test-worker",
            body="Must achieve X.", goal_mode=True,
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    return goal_task_id


def test_block_goal_mode_rejects_missing_kind(monkeypatch, tmp_path):
    """A goal_mode worker calling kanban_block with no kind must not be able
    to use it as an unguarded escape from the goal loop (Issue #38696,
    sibling of the kanban_complete judge gate / Issue #38367)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "giving up"})
    d = json.loads(out)
    assert "error" in d
    assert "goal_mode" in d["error"]

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_rejects_disallowed_kind(monkeypatch, tmp_path):
    """`capability` / `transient` are valid kinds in general but must not
    let a goal_mode worker exit the loop without going through the judge."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    for kind in ("capability", "transient"):
        out = kt._handle_block({"reason": "blocked", "kind": kind})
        d = json.loads(out)
        assert "error" in d, f"kind={kind} should be rejected for goal_mode"

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_heartbeat_extends_claim_expires(worker_env):
    """The kanban_heartbeat tool MUST extend claim_expires, not just
    update last_heartbeat_at — otherwise long-running workers loop the
    heartbeat tool diligently and still get reclaimed by
    release_stale_claims at DEFAULT_CLAIM_TTL_SECONDS.

    Regression test for the bug where _handle_heartbeat called
    heartbeat_worker but never heartbeat_claim, so claim_expires sat
    static while last_heartbeat_at advanced.
    """
    import time as _time
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    # Rewind claim_expires into the past so any forward movement is
    # unambiguous (avoids time.sleep flakiness).
    conn = kbc.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (1, worker_env),
        )
        conn.commit()
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()
    assert before == 1

    out = kt._handle_heartbeat({"note": "still alive"})
    assert json.loads(out).get("ok") is True

    conn = kbc.connect()
    try:
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()

    now = int(_time.time())
    # claim_expires should be roughly now + DEFAULT_CLAIM_TTL_SECONDS.
    # We assert a generous floor (now + half the default TTL) to keep the
    # test stable against future TTL changes.
    assert after > before, (
        f"claim_expires did not advance ({before} -> {after}); workers "
        f"would be reclaimed at TTL despite heartbeating"
    )
    assert after >= now + (kb.DEFAULT_CLAIM_TTL_SECONDS // 2), (
        f"claim_expires={after} is suspiciously close to now={now}; "
        f"expected at least now + {kb.DEFAULT_CLAIM_TTL_SECONDS // 2}"
    )


def test_comment_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env,
        "body": "hello thread",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["comment_id"]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        assert len(comments) == 1
        # Author defaults to HERMES_PROFILE env we set in the fixture
        assert comments[0].author == "test-worker"
        assert comments[0].body == "hello thread"
    finally:
        conn.close()


def test_comment_ignores_caller_supplied_author(worker_env):
    """``args["author"]`` is no longer honored — the author is always
    derived from ``HERMES_PROFILE`` so a worker can't forge a comment
    under an authoritative-looking name like ``hermes-system`` and
    poison the next worker's prompt context. Cross-task commenting
    itself remains unrestricted (see #19713); only the author override
    is removed.
    """
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env, "body": "hi", "author": "hermes-system",
    })
    assert json.loads(out)["ok"]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        # Author comes from HERMES_PROFILE in the fixture, not the
        # caller-supplied "hermes-system" override.
        assert comments[0].author == "test-worker"
    finally:
        conn.close()


def test_create_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"]
    assert d["status"] == "todo"  # parent isn't done yet
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.title == "child task"
        assert child.assignee == "peer"
    finally:
        conn.close()


def test_link_happy_path(worker_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": a, "child_id": b})
    d = json.loads(out)
    assert d["ok"] is True


def test_unblock_happy_path(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        kb.block_task(conn, tid, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": tid})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_unblock_with_pending_parents_returns_todo(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": child})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "todo"

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "todo"
    finally:
        conn.close()


def test_worker_lifecycle_through_tools(worker_env):
    """Drive the full claim -> heartbeat -> comment -> complete lifecycle
    exclusively through the tools, then verify the DB state matches what
    the dispatcher/notifier expect."""
    from tools import kanban_tools as kt

    # 1. show — worker orientation
    show = json.loads(kt._handle_show({}))
    assert show["task"]["id"] == worker_env

    # 2. heartbeat during long op
    assert json.loads(kt._handle_heartbeat({"note": "warming up"}))["ok"]

    # 3. comment for a future peer
    assert json.loads(kt._handle_comment({
        "task_id": worker_env,
        "body": "note: using stdlib sqlite3 bindings",
    }))["ok"]

    # 4. spawn a child task for follow-up
    child_out = json.loads(kt._handle_create({
        "title": "write integration test",
        "assignee": "qa",
        "parents": [worker_env],
    }))
    assert child_out["ok"]

    # 5. complete with structured handoff
    comp = json.loads(kt._handle_complete({
        "summary": "implemented + spawned QA follow-up",
        "metadata": {"child_task": child_out["task_id"]},
    }))
    assert comp["ok"]

    # Verify final state
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent.status == "done"
        assert parent.current_run_id is None
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.metadata == {"child_task": child_out["task_id"]}
        # Child is todo (parent just finished, but recompute_ready may
        # have promoted it — complete_task runs recompute internally).
        child = kb.get_task(conn, child_out["task_id"])
        assert child.status == "ready", (
            f"child should be ready after parent done, got {child.status}"
        )
        # Comment is visible
        assert len(kb.list_comments(conn, worker_env)) == 1
        # Heartbeat event recorded
        hb = [e for e in kb.list_events(conn, worker_env) if e.kind == "heartbeat"]
        assert len(hb) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# System-prompt guidance injection
# ---------------------------------------------------------------------------


def test_kanban_guidance_prompt_size_bounded():
    """KANBAN_GUIDANCE is injected into every kanban-capable process's system
    prompt and resolved once at agent init, so its size is a per-worker token
    tax paid on every spawn. Bound it as an invariant, not a change-detector:
    the ceiling (8000 chars, roughly 2000 tokens) leaves headroom above the
    current ~6.2k chars for tight additions, while catching accidental bloat
    (pasted docs, duplicated sections) before it ships to every worker.
    """
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert len(KANBAN_GUIDANCE) < 8000, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars; it is injected into "
        "every kanban worker's system prompt — trim it or consciously re-bound "
        "this invariant with justification."
    )


def test_kanban_guidance_orchestrator_decision_ownership():
    """The orchestrator section must carry the split-brain prevention
    contract: decisions are made by the orchestrator before fan-out and
    stamped into every dependent card body."""
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert KANBAN_GUIDANCE.count("Decision ownership.") == 1
    assert "Never let two subtree cards decide the same question" in KANBAN_GUIDANCE
    assert "workers cannot see sibling context" in KANBAN_GUIDANCE


# ---------------------------------------------------------------------------
# Worker task-ownership enforcement (regression tests for #19534)
# ---------------------------------------------------------------------------
#
# A worker process has HERMES_KANBAN_TASK set to its own task id. The
# destructive tools (kanban_complete, kanban_block, kanban_heartbeat,
# kanban_unblock) must refuse to operate
# on any OTHER task id, even if the caller supplies an explicit `task_id`
# argument. Workers legitimately call kanban_show / kanban_list /
# kanban_comment / kanban_create / kanban_link on other tasks, so those
# are unrestricted.
#
# Orchestrator profiles (no HERMES_KANBAN_TASK in env) are intentionally
# exempt — their job is routing, and they sometimes close out child
# tasks on behalf of the child.


def test_worker_complete_rejects_foreign_task_id(worker_env):
    """A worker cannot complete a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": other, "summary": "HIJACK"})
    d = json.loads(out)
    assert d.get("ok") is not True
    assert "refusing to mutate" in d.get("error", "")

    # Sibling task must be untouched.
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_can_comment_on_foreign_task(worker_env):
    """Cross-task commenting must remain unrestricted (#19713 policy).

    The author-forgery hardening removed args['author'] but deliberately
    did NOT add an ownership gate to kanban_comment — comments are the
    documented handoff channel between tasks. This test pins that policy
    so a future change accidentally adding ``_enforce_worker_task_ownership``
    to ``_handle_comment`` would fail CI immediately.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        other = kb.create_task(conn, title="sibling")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": other,
        "body": "handoff: see prior findings before starting",
    })
    d = json.loads(out)
    assert d.get("ok") is True, f"cross-task comment must succeed: {d}"

    # The comment lands on the foreign task, attributed to the worker's
    # HERMES_PROFILE — never to a caller-controlled string.
    conn = kbc.connect()
    try:
        comments = kb.list_comments(conn, other)
        assert len(comments) == 1
        assert comments[0].author == "test-worker"
        assert comments[0].body.startswith("handoff:")
    finally:
        conn.close()


def test_worker_unblock_rejects_foreign_task_id(worker_env):
    """A worker cannot unblock any task — kanban_unblock is orchestrator-only.

    The check fires before the per-task ownership check, so the error
    surface is the orchestrator-only refusal rather than the
    cross-task-ownership refusal. Either is fine — the property we're
    pinning is "worker cannot mutate foreign task via kanban_unblock".
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        other = kb.create_task(conn, title="blocked sibling", assignee="peer")
        kb.block_task(conn, other, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": other})
    d = json.loads(out)
    err = d.get("error", "")
    assert "orchestrator-only" in err or "refusing to mutate" in err, (
        f"expected worker-rejection error, got {err}"
    )

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
    finally:
        conn.close()


def test_orchestrator_complete_any_task_allowed(monkeypatch, tmp_path):
    """Orchestrator profiles (no HERMES_KANBAN_TASK) can still complete
    any task via explicit task_id. The check only applies to workers."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="child to close out")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": tid, "summary": "orchestrator close"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == tid


# ---------------------------------------------------------------------------
# Optional ``board`` parameter — per-call DB override
# ---------------------------------------------------------------------------
#
# The dispatcher pins the active board via HERMES_KANBAN_BOARD env var,
# but a Telegram-side orchestrator handling multiple boards needs to be
# able to route a single tool call to a specific board's DB without
# restarting Hermes. These tests pin that ``board=<slug>`` argument
# routes each handler to that board's sqlite file, and that omitting
# ``board`` preserves the legacy env-driven resolution.


@pytest.fixture
def multi_board_env(monkeypatch, tmp_path):
    """Isolated Hermes home with two distinct kanban boards seeded.

    Returns ``("default", "alt")`` slugs. The default board has one
    pre-existing task ``seed_default``; ``alt`` has ``seed_alt``. No
    HERMES_KANBAN_TASK is pinned (orchestrator context) — workers test
    the env-task case via the existing ``worker_env`` fixture.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Make sure neither HERMES_KANBAN_DB nor HERMES_KANBAN_BOARD pin a
    # board — the test is specifically about the per-call override.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    # Default board — implicit
    conn = kbc.connect()
    try:
        seed_default = kb.create_task(
            conn, title="seed-default", assignee="worker-d"
        )
    finally:
        conn.close()
    # Alt board — explicit slug routes the connection to a separate DB
    conn = kbc.connect(board="alt")
    try:
        seed_alt = kb.create_task(
            conn, title="seed-alt", assignee="worker-a"
        )
    finally:
        conn.close()
    return {
        "default_seed": seed_default,
        "alt_seed": seed_alt,
        "default_db": kb.kanban_db_path(),
        "alt_db": kb.kanban_db_path(board="alt"),
    }


def test_board_param_none_falls_back_to_env(worker_env):
    """When ``board`` is omitted or None, behaviour is unchanged from
    before this feature — calls land on whatever the env resolves to.
    Regression guard against accidentally rewiring default resolution."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_show({})  # no board, no task_id
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    out = kt._handle_show({"task_id": worker_env, "board": None})
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    # Sanity: the env-resolved path is the legacy default DB, NOT an
    # 'alt' board path. Confirms the override path was not silently
    # forced.
    assert kb.kanban_db_path() == kb.kanban_db_path(board="default")


# ---------------------------------------------------------------------------
# kanban_create auto-subscribe behaviour
#
# When a worker calls kanban_create from inside a session that has a
# persistent delivery channel, the originating session should be
# subscribed to the new task's completion/block events automatically.
# - Gateway sessions: HERMES_SESSION_PLATFORM + HERMES_SESSION_CHAT_ID set.
# - TUI sessions: HERMES_SESSION_KEY (or HERMES_SESSION_ID) set, with
#   the platform/chat_id ContextVars intentionally empty.
# - CLI / cron / test sessions: no delivery channel -> no subscription.
# - Config gate kanban.auto_subscribe_on_create: false -> no subscription
#   even when the session has a delivery channel.
# ---------------------------------------------------------------------------

def _list_subs_for_task(task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    conn = kbc.connect()
    try:
        return list(kbn.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def _sub_index(subs):
    """Normalise a list of notify-subs (dicts or objects) into dicts
    keyed by platform+chat_id, so assertions work regardless of the
    return shape."""
    out = []
    for s in subs:
        if isinstance(s, dict):
            out.append(s)
        else:
            out.append({
                "platform": getattr(s, "platform", None),
                "chat_id": getattr(s, "chat_id", None),
                "thread_id": getattr(s, "thread_id", None),
                "user_id": getattr(s, "user_id", None),
                "delivery_metadata": getattr(s, "delivery_metadata", None),
                "notifier_profile": getattr(s, "notifier_profile", None),
            })
    return out


def test_create_subscribes_gateway_session(monkeypatch, worker_env):
    """A gateway session (platform + chat_id set) gets auto-subscribed
    to its own kanban_create result, and the response surfaces the
    ``subscribed`` flag so the orchestrator can react."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-7")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user-9")
    monkeypatch.setenv("HERMES_SESSION_USER_ID_ALT", "alt-user-9")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "forum")

    out = kt._handle_create({
        "title": "auto-sub gateway",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "telegram"
    assert s["chat_id"] == "chat-42"
    assert s["thread_id"] == "thread-7"
    assert s["user_id"] == "user-9"
    assert s["user_id_alt"] == "alt-user-9"
    assert s["chat_type"] == "forum"
    assert s["delivery_mode"] == "notify+wake"


def test_create_subscribes_tui_session_via_session_key(monkeypatch, worker_env):
    """TUI / desktop sessions don't have a platform/chat_id (single
    local channel), but the parent process exports HERMES_SESSION_KEY.
    We should still auto-subscribe, with platform='tui' and
    chat_id=<key>."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-abc")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "auto-sub tui",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-session-abc"
    assert subs[0]["chat_type"] == "dm"
    assert subs[0]["delivery_mode"] == "notify"


def test_create_does_not_subscribe_in_cli_session(monkeypatch, worker_env):
    """CLI / cron / test sessions have no persistent delivery channel.
    _maybe_auto_subscribe returns False and no row is written."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub cli",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_respects_auto_subscribe_on_create_false(monkeypatch, worker_env, tmp_path):
    """The config gate kanban.auto_subscribe_on_create=false must
    suppress auto-subscription even when the session has a delivery
    channel. This is the knob that addresses the upstream design
    concern from PR #19718 (reverted in #19721) — users who want
    explicit kanban_notify-subscribe calls per task get that."""
    # worker_env already created <tmp>/.hermes; use a fresh sibling
    # home to avoid mkdir() colliding with the worker's directory.
    home = tmp_path / "gate-home" / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  auto_subscribe_on_create: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "channel-1")

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "no sub gated",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_maybe_auto_subscribe_swallows_add_notify_sub_failure(monkeypatch, worker_env):
    """If add_notify_sub itself raises (e.g. DB locked, schema drift),
    _maybe_auto_subscribe must NOT bubble that up and fail the parent
    kanban_create. The function returns False and the parent create
    still succeeds with subscribed=False."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_notify as kbn

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kbn, "add_notify_sub", _boom)

    out = kt._handle_create({
        "title": "auto-sub tolerates add_notify_sub failure",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    assert d["subscribed"] is False, d


# ---------------------------------------------------------------------------
# Attachments — kanban_attach / kanban_attach_url / kanban_attachments
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_private_urls(monkeypatch):
    """Opt the SSRF guard into private/loopback targets for local fixtures.

    Mirrors a user setting HERMES_ALLOW_PRIVATE_URLS on a private network.
    Resets the url_safety process-lifetime cache on both sides so the
    override neither leaks in nor out of the test.
    """
    from tools import url_safety

    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def test_attach_url_rejects_non_http_scheme(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": "file:///etc/passwd"})
    d = json.loads(out)
    assert "error" in d
    assert "scheme" in d["error"]


# ---------------------------------------------------------------------------
# kanban_attach_url — SSRF guard (tools/url_safety.is_safe_url per hop)
# ---------------------------------------------------------------------------


@pytest.fixture
def default_url_guard(monkeypatch):
    """Force the SSRF guard to its secure default for this test.

    Clears HERMES_ALLOW_PRIVATE_URLS and resets url_safety's process-lifetime
    cache on both sides so a prior test's opt-in can't leak in.
    """
    from tools import url_safety

    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def _assert_attach_url_blocked(worker_env, url):
    """Call kanban_attach_url with ``url`` and assert the SSRF guard fired
    (clean tool error, no attachment row, no network fetch needed)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": url})
    d = json.loads(out)
    assert "error" in d, out
    assert "SSRF" in d["error"] or "blocked" in d["error"].lower(), out
    conn = kbc.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_blocks_loopback(worker_env, default_url_guard):
    """http://127.0.0.1/ is rejected before any connection is made."""
    _assert_attach_url_blocked(worker_env, "http://127.0.0.1/")


def _fake_public_dns(monkeypatch, mapping):
    """Patch url_safety's getaddrinfo so hostnames in ``mapping`` resolve to
    the given (public) IPs and literal IPs resolve to themselves — no real
    DNS or network traffic."""
    import ipaddress
    import socket as _socket

    real_af, real_sock = _socket.AF_INET, _socket.SOCK_STREAM

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            # Literal IPs pass through; unknown hostnames fail like NXDOMAIN.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise _socket.gaierror(f"fake DNS: unknown host {host!r}")
            ip = host
        return [(real_af, real_sock, 6, "", (ip, 0))]

    from tools import url_safety
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeStreamResponse:
    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in {
            k.lower() for k in self.headers
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_attach_url_happy_path_public_host(worker_env, default_url_guard, monkeypatch):
    """A public URL passes the guard and the bytes are stored (mocked fetch)."""
    from pathlib import Path

    import httpx

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    payload = b"public fetch body"

    def fake_stream(method, url, **kwargs):
        assert url == "http://files.example.com/docs/spec.pdf"
        return _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "application/pdf; charset=binary"},
            body=payload,
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/docs/spec.pdf"})
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kbc.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["spec.pdf"]
        assert atts[0].content_type == "application/pdf"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reviewer escalation via the real kanban_block tool path (task
# t_f4ab1544, acceptance criterion 4: reviewer escalation is a legal
# terminal action through the real tool path).
# ---------------------------------------------------------------------------


@pytest.fixture
def review_claim_env(monkeypatch, tmp_path):
    """A worker env whose task is claimed by a REVIEWER (not the original
    implementer): request_review -> claim_review_task, then
    HERMES_KANBAN_TASK is pointed at that claimed run."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "reviewer")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="reviewer-escalation-test", assignee="builder")
        implementation = kb.claim_task(conn, tid, claimer="builder:1")
        assert implementation is not None
        assert kb.request_review(
            conn, tid, summary="ready", reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, tid, claimer="reviewer:1")
        assert review is not None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_reviewer_escalates_via_real_kanban_block_tool(review_claim_env):
    """An active reviewer must be able to escalate through the real
    ``kanban_block`` tool/handler, and an explicit unblock must resume
    the task in ``review`` (not ``ready``) — the reviewer's escalation
    is a legal terminal action, distinct from an implementer's block."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    out = kt._handle_block({
        "reason": "needs_input: maintainer decision required",
        "kind": "needs_input",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "blocked"

    conn = kbc.connect()
    try:
        blocked = kb.get_task(conn, review_claim_env)
        assert blocked is not None
        assert blocked.status == "blocked"
        events = kb.list_events(conn, review_claim_env)
        blocked_event = [e for e in events if e.kind == "blocked"][-1]
        assert blocked_event.payload is not None
        assert blocked_event.payload.get("source_status") == "review"

        assert kb.unblock_task(conn, review_claim_env)
        resumed = kb.get_task(conn, review_claim_env)
        assert resumed is not None
        assert resumed.status == "review"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Roadmap lanes — kanban_create(lane=...) and the kanban_roadmap tool
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator_env(monkeypatch, tmp_path):
    """An orchestrator profile: isolated HERMES_HOME, no HERMES_KANBAN_TASK, so the
    orchestrator-gated lane tools are reachable."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.mark.parametrize("lane", ["idea", "roadmap"])
def test_tool_create_lane_needs_no_assignee(orchestrator_env, lane):
    """A wishlist card never dispatches, so the tool drops the assignee requirement
    that exists to stop work parking unassigned in ready forever."""
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "wishlist item", "lane": lane}))
    assert d["ok"] is True
    assert d["status"] == lane

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, d["task_id"]).assignee is None


def test_tool_create_still_requires_assignee_without_a_lane(orchestrator_env):
    """The relaxation is scoped to lane cards — real work still needs an assignee."""
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "real work"}))
    assert d.get("ok") is not True
    assert "assignee is required" in d.get("error", "")


def test_tool_create_rejects_an_unknown_lane(orchestrator_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "x", "lane": "backlog"}))
    assert d.get("ok") is not True
    assert "lane must be" in d.get("error", "")


def test_tool_roadmap_refine_demote_spawn(orchestrator_env):
    """The action-style lane tool moves a card through both lanes and into triage."""
    from tools import kanban_tools as kt
    tid = json.loads(kt._handle_create({"title": "wish", "lane": "idea"}))["task_id"]

    assert json.loads(kt._handle_roadmap({"task_id": tid, "action": "refine"}))["status"] == "roadmap"
    assert json.loads(kt._handle_roadmap({"task_id": tid, "action": "demote"}))["status"] == "idea"
    kt._handle_roadmap({"task_id": tid, "action": "refine"})
    assert json.loads(kt._handle_roadmap({"task_id": tid, "action": "spawn"}))["status"] == "triage"


def test_tool_roadmap_spawn_to_ready(orchestrator_env):
    from tools import kanban_tools as kt
    tid = json.loads(kt._handle_create({"title": "wish", "lane": "roadmap"}))["task_id"]
    d = json.loads(kt._handle_roadmap({"task_id": tid, "action": "spawn", "to": "ready"}))
    assert d["status"] == "ready"


def test_tool_roadmap_refuses_live_work_and_says_why(orchestrator_env):
    """A refused lane move surfaces the DB layer's from->to message as a tool error and
    leaves the live card untouched."""
    from tools import kanban_tools as kt
    tid = json.loads(kt._handle_create({"title": "real work", "assignee": "peer"}))["task_id"]
    d = json.loads(kt._handle_roadmap({"task_id": tid, "action": "refine"}))
    assert d.get("ok") is not True
    assert "-> 'roadmap'" in d.get("error", "")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_tool_roadmap_is_orchestrator_only(worker_env):
    """A dispatcher-spawned task worker must not be able to move wishlist cards."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="wish", lane="idea")

    from tools import kanban_tools as kt
    d = json.loads(kt._handle_roadmap({"task_id": tid, "action": "refine"}))
    assert d.get("ok") is not True
    assert "orchestrator-only" in d.get("error", "") or "refusing to mutate" in d.get("error", "")

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "idea"


# ---------------------------------------------------------------------------
# Mergeability preflight on kanban_request_review (task t_3e83300c).
#
# Real git repositories throughout: the whole point of the preflight is that
# git's own merge machinery decides, so a mocked ``git`` would assert nothing.
# ---------------------------------------------------------------------------


def _git(repo, *args: str) -> str:
    import subprocess
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_origin(root):
    """A repo with ``main`` (base) and ``dev`` (base + an edit to f.txt)."""
    origin = root / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    (origin / "f.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")
    base = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "-q", "-b", "dev")
    (origin / "f.txt").write_text("line1-FROM-DEV\nline2\n", encoding="utf-8")
    _git(origin, "commit", "-qam", "dev moves f.txt")
    _git(origin, "checkout", "-q", "main")
    return origin, base


def _make_workspace(root, origin, base, *, conflicting: bool):
    """A clone branched off ``base``; ``conflicting`` decides whether its edit
    collides with what ``origin/dev`` did to the same line."""
    ws = root / "ws"
    _git(root, "-c", "init.defaultBranch=main", "clone", "-q", str(origin), str(ws))
    _git(ws, "config", "user.email", "t@example.invalid")
    _git(ws, "config", "user.name", "t")
    _git(ws, "checkout", "-q", "-b", "feature", base)
    if conflicting:
        (ws / "f.txt").write_text("line1-FROM-FEATURE\nline2\n", encoding="utf-8")
    else:
        (ws / "untouched-by-dev.txt").write_text("safe\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feature work")
    return ws


@pytest.fixture
def mergeability_env(monkeypatch, tmp_path):
    """Factory: build a worker task whose workspace is a real git clone, on a
    board with a real ``land_target``. Returns ``make(conflicting=...)`` ->
    ``(task_id, workspace_path)``."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    repos = tmp_path / "repos"
    repos.mkdir()
    origin, base = _make_origin(repos)

    def make(*, conflicting: bool, land_target: str = "origin/dev",
             workspace_path=None, status: str = "running"):
        """``status`` selects the card state under test: ``running`` (claimed,
        the ordinary worker case), ``ready`` (never claimed), ``todo`` (held by
        an unfinished parent), or ``done`` (claimed then completed)."""
        ws = _make_workspace(repos, origin, base, conflicting=conflicting)
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        if land_target:
            kb.write_board_metadata(None, land_target=land_target)
        with kbc.connect_closing() as conn:
            parents = ()
            if status == "todo":
                parents = (kb.create_task(
                    conn, title="unfinished parent", assignee="test-worker"),)
            tid = kb.create_task(
                conn, title="mergeability", assignee="test-worker",
                workspace_kind="worktree", parents=parents,
                workspace_path=str(ws if workspace_path is None else workspace_path))
            claimed = None
            if status in ("running", "done"):
                claimed = kb.claim_task(conn, tid)
                assert claimed is not None
            if status == "done":
                assert kb.complete_task(
                    conn, tid, summary="done", expected_run_id=claimed.current_run_id)
            assert kb.get_task(conn, tid).status == status
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        # request_review only clears a live claim with proof of ownership, which
        # the real dispatcher supplies through this env var at spawn time.
        if claimed is not None:
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        else:
            monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        return tid, ws

    return make


def _events(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.list_events(conn, tid)


def test_request_review_refuses_a_branch_that_conflicts_with_the_land_target(
    mergeability_env,
):
    """AC1: a worktree whose HEAD conflicts with origin/<land_target> cannot
    enter the review lane. The refusal names the conflicting path and the
    exact fix command, the card stays running, and the refusal is recorded
    as a ``review_preflight_conflict`` event so it can be counted."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid, _ws = mergeability_env(conflicting=True)

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))

    assert d.get("ok") is not True
    error = d.get("error", "")
    assert "f.txt" in error, error
    assert "git merge origin/dev" in error, error

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"

    conflicts = [e for e in _events(tid) if e.kind == "review_preflight_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0].payload["target"] == "origin/dev"
    assert conflicts[0].payload["paths"] == ["f.txt"]


def test_request_review_stamps_the_target_it_verified_when_the_branch_merges(
    mergeability_env,
):
    """AC2: a clean-merging worktree is handed off exactly as before, plus the
    proof of what it was checked against — the reviewer reads the target and
    commit from the event instead of taking the worker's word for it."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid, ws = mergeability_env(conflicting=False)

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))
    assert d["ok"] is True
    assert d["status"] == "review"

    requested = [e for e in _events(tid) if e.kind == "review_requested"]
    assert len(requested) == 1
    # request_review stores the handoff metadata on the run the event points at
    # (task_runs.metadata), not inline on the event payload.
    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, requested[0].run_id)
    assert run is not None
    stamp = run.metadata["mergeable_against"]

    target, _, sha = stamp.partition("@")
    assert target == "origin/dev"
    assert sha == _git(ws, "rev-parse", "origin/dev")

    assert not [e for e in _events(tid) if e.kind == "review_preflight_conflict"]


def _assert_untouched_handoff(tid):
    """AC3's shared contract: the handoff behaved exactly as it did before the
    preflight existed — it succeeded, stamped nothing, and recorded nothing."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    requested = [e for e in _events(tid) if e.kind == "review_requested"]
    assert len(requested) == 1
    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, requested[0].run_id)
    assert run is not None
    assert "mergeable_against" not in (run.metadata or {})
    assert not [e for e in _events(tid) if e.kind == "review_preflight_conflict"]


def test_request_review_ignores_the_conflict_when_the_preflight_is_disabled(
    mergeability_env, monkeypatch,
):
    """AC3: ``kanban.require_mergeable_for_review: false`` is a real off switch —
    the same branch that AC1 refuses is handed off untouched."""
    from tools import kanban_tools as kt

    tid, _ws = mergeability_env(conflicting=True)
    monkeypatch.setattr(
        kt._ktm, "cfg_get",
        lambda cfg, *keys, default=None: (
            False if keys == ("kanban", "require_mergeable_for_review") else default))

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))
    assert d["ok"] is True
    assert d["status"] == "review"
    _assert_untouched_handoff(tid)


def test_request_review_skips_the_preflight_when_the_board_has_no_land_target(
    mergeability_env,
):
    """AC3: with no ``land_target`` there is nothing to merge against, and the
    preflight must not invent one (no guessing ``dev``/``main``, no reading the
    branch's upstream). The same conflicting branch is handed off untouched."""
    from tools import kanban_tools as kt

    tid, _ws = mergeability_env(conflicting=True, land_target="")

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))
    assert d["ok"] is True
    assert d["status"] == "review"
    _assert_untouched_handoff(tid)


def test_request_review_skips_the_preflight_when_the_workspace_is_not_a_git_repo(
    mergeability_env, tmp_path,
):
    """AC3: a scratch (non-git) workspace has no HEAD to merge, so the preflight
    fails open rather than refusing work it cannot judge."""
    from tools import kanban_tools as kt

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    tid, _ws = mergeability_env(conflicting=True, workspace_path=plain)

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))
    assert d["ok"] is True
    assert d["status"] == "review"
    _assert_untouched_handoff(tid)


def test_request_review_fails_open_when_the_land_target_cannot_be_fetched(
    mergeability_env, tmp_path,
):
    """An unreachable remote is an infrastructure problem, not a verdict on the
    branch. The preflight must never strand finished work outside the review
    lane because the network (or a renamed remote) was down."""
    from tools import kanban_tools as kt

    tid, ws = mergeability_env(conflicting=True)
    _git(ws, "remote", "set-url", "origin", str(tmp_path / "gone"))

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))
    assert d["ok"] is True
    assert d["status"] == "review"
    _assert_untouched_handoff(tid)


# ---------------------------------------------------------------------------
# Gate ordering: status before mergeability (task t_fd4e3978).
#
# The preflight used to run in front of the status check, so a card that could
# not enter the review lane at all was answered with a merge-conflict refusal
# that additionally asserted it was "still running". Same conflicting worktree,
# same gate ON — only the card's status varies.
# ---------------------------------------------------------------------------


def _assert_status_answer_not_merge_refusal(error: str) -> None:
    """The refusal must be about the card's state, not about git. Asserted
    negatively too: naming the conflicting path or the fix command would mean
    the merge gate answered a question it has no business answering."""
    assert "f.txt" not in error, error
    assert "git merge origin/dev" not in error, error
    assert "still running" not in error, error


def test_request_review_on_a_done_card_answers_status_not_mergeability(
    mergeability_env,
):
    """A completed card is not the worker's to hand off. The refusal must say
    so — before the fix it got the merge-conflict text, which sent the reader
    to resolve a conflict AND claimed the card was "still running"."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid, _ws = mergeability_env(conflicting=True, status="done")

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))

    assert d.get("ok") is not True
    error = d.get("error", "")
    _assert_status_answer_not_merge_refusal(error)
    assert "running/ready" in error, error

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
    assert not [e for e in _events(tid) if e.kind == "review_preflight_conflict"]


def test_request_review_on_a_todo_card_answers_status_not_mergeability(
    mergeability_env,
):
    """A never-claimed card held in ``todo`` by an unfinished parent is gated
    on that parent, not on git. The merge gate must not speak for it."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid, _ws = mergeability_env(conflicting=True, status="todo")

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))

    assert d.get("ok") is not True
    error = d.get("error", "")
    _assert_status_answer_not_merge_refusal(error)
    assert "parent" in error, error

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "todo"
    assert not [e for e in _events(tid) if e.kind == "review_preflight_conflict"]


def test_request_review_refusal_states_the_status_the_card_is_actually_in(
    mergeability_env,
):
    """``ready`` is reviewable, so a conflicting ``ready`` card is still
    correctly refused by the merge gate — but the refusal must describe the
    card it is holding. "still running" about a ``ready`` card is the same
    confidently-wrong sentence the reorder removes elsewhere."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid, _ws = mergeability_env(conflicting=True, status="ready")

    d = json.loads(kt._handle_request_review({"summary": "implemented the thing"}))

    assert d.get("ok") is not True
    error = d.get("error", "")
    assert "f.txt" in error, error
    assert "still ready" in error, error
    assert "still running" not in error, error

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
    assert len([e for e in _events(tid) if e.kind == "review_preflight_conflict"]) == 1
