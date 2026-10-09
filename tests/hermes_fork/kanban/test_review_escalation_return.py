"""Diagnosis hop: an escalated rework returns to the implementer on its own.

``kanban.review_rework_escalation_profile`` may name a diagnose-only profile
(``debugger``). Its worker ends with a comment + block (or a complete), and
``hermes_fork.kanban.review_routing.return_diagnosed`` (run every tick from the
``start-budget`` anchor) must hand the card back to the implementer in
``ready`` with the diagnosis in context, without counting a review round.

Drives the real ``dispatch_once`` on an isolated HERMES_HOME/kanban.db, like
``test_review_routing.py``; ``spawn_fn`` only records the profile.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_fork.kanban import review_routing

from tests.hermes_fork.kanban.test_review_routing import (  # noqa: F401  (fixture)
    _events,
    _hand_in,
    _review_and_reject,
    _start,
    _task,
    _tick,
    board,
)

DIAGNOSIS = "Root cause: reconciler resumes with stale policy (cutover.sh:373). Fix: fail closed."


def _escalated(conn, cfg, spawn, spawned, *, cap=4):
    """Card on round 2 with the rework now running under ``debugger``."""
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=cap)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"      # round 1
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"   # round 2 -> escalate
    t = _task(conn, tid)
    assert (t.status, t.assignee) == ("running", "debugger")
    return tid


def _diagnose_and_block(conn, tid, kind="transient"):
    """What the debugger's worker does: comment the diagnosis, then block."""
    t = _task(conn, tid)
    kb.add_comment(conn, tid, "debugger", DIAGNOSIS)
    assert kb.block_task(conn, tid, reason="ACTION: hand back to @coder: stale policy on rollback", kind=kind,
                         expected_run_id=t.current_run_id)


# ------------------------------------------------------------- happy path

def test_escalation_records_return_path_and_briefs_the_escalated_worker(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    assert _events(conn, tid, "assigned")[-1]["return_to"] == "coder"
    ctx = kb.build_worker_context(conn, tid)  # what the debugger worker was given
    assert "Review escalation: 2 review round(s)" in ctx
    assert 'kind="transient"' in ctx and 'Do NOT use kind="capability"' in ctx
    assert "ACTION: hand back to @coder" in ctx


def test_escalate_diagnose_block_auto_returns_then_reviews(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    assert _task(conn, tid).status == "blocked"

    # Next tick: handed back and spawned under the implementer, no operator.
    assert _tick(conn, spawn, spawned) == "coder"
    t = _task(conn, tid)
    assert (t.status, t.assignee, t.block_kind) == ("running", "coder", None)
    [marker] = _events(conn, tid, review_routing.RETURNED_EVENT)
    assert marker["return_to"] == "coder" and marker["from"] == "debugger"
    assert (marker["terminal"], marker["block_kind"], marker["status"]) == ("blocked", "transient", "ready")
    assert _events(conn, tid, "assigned")[-1] == {
        "assignee": "coder", "from": "debugger", "source": "kanban.review_rework_return",
    }

    # The diagnosis reaches the implementer's context: comment + the blocked attempt.
    ctx = kb.build_worker_context(conn, tid)
    assert DIAGNOSIS in ctx
    assert "ACTION: hand back to @coder: stale policy on rollback" in ctx
    assert "Rework handed back to @coder after @debugger's diagnosis" in ctx

    # The hop is not a review round, and it does not re-escalate this round.
    rounds, _latest, _marker = review_routing._loop_state(conn, tid)
    assert rounds == 2

    # Implementer fixes -> reviewer (still the prior reviewer) -> approve.
    _hand_in(conn, tid)
    assert _tick(conn, spawn, spawned) == "reviewer"
    assert kb.complete_task(conn, tid, summary="approved")


def test_capability_or_loop_detected_block_is_still_a_hand_back(board):
    """The t_66deb47b stall: a second same-kind block lands in ``triage``."""
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    conn.execute("UPDATE tasks SET block_kind = 'capability', block_recurrences = 1 WHERE id = ?", (tid,))
    conn.commit()
    _diagnose_and_block(conn, tid, kind="capability")
    assert _task(conn, tid).status == "triage"
    assert _tick(conn, spawn, spawned) == "coder"
    assert _events(conn, tid, review_routing.RETURNED_EVENT)[0]["terminal"] == "block_loop_detected"


def test_complete_with_diagnosis_reopens_returns_and_regates_children(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    child = kb.create_task(conn, title="child", assignee="operator", parents=[tid])
    assert _task(conn, child).status == "todo"

    t = _task(conn, tid)
    assert kb.complete_task(conn, tid, summary=f"Diagnosis only. {DIAGNOSIS}",
                            expected_run_id=t.current_run_id)
    assert _task(conn, child).status == "ready"  # released by the diagnosis completion

    # One tick: the card goes home and the child is re-gated BEFORE lanes run.
    assert _tick(conn, spawn, spawned) == "coder"
    assert not [s for s in spawned if s[0] == child]
    assert _task(conn, child).status == "todo"
    t = _task(conn, tid)
    assert (t.status, t.assignee, t.completed_at) == ("running", "coder", None)
    assert DIAGNOSIS in kb.build_worker_context(conn, tid)
    # A diagnosis completion is not an approval: the round count survives.
    rounds, _latest, _marker = review_routing._loop_state(conn, tid)
    assert rounds == 2


# ------------------------------------------------------------ not handed back

def test_needs_input_block_stays_for_the_operator(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid, kind="needs_input")
    assert _tick(conn, spawn, spawned) is None
    t = _task(conn, tid)
    assert (t.status, t.assignee) == ("blocked", "debugger")
    assert not _events(conn, tid, review_routing.RETURNED_EVENT)


def test_operator_reassign_after_escalation_block_wins(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    assert kb.assign_task(conn, tid, "operator")
    assert _tick(conn, spawn, spawned) is None
    t = _task(conn, tid)
    assert (t.status, t.assignee) == ("blocked", "operator")
    assert not _events(conn, tid, review_routing.RETURNED_EVENT)


def test_operator_unblock_after_escalation_block_wins(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    assert kb.unblock_task(conn, tid)  # human: "debugger, go again"
    assert _tick(conn, spawn, spawned) == "debugger"
    assert not _events(conn, tid, review_routing.RETURNED_EVENT)


def test_operator_unblock_then_second_block_still_wins(board):
    """Operator said "debugger, go again"; its next block is not a hand-back."""
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    assert kb.unblock_task(conn, tid)
    assert _tick(conn, spawn, spawned) == "debugger"
    _diagnose_and_block(conn, tid)
    assert _tick(conn, spawn, spawned) is None
    t = _task(conn, tid)
    # A second same-kind block goes to ``triage`` upstream; either way it stays put.
    assert t.status in ("blocked", "triage") and t.assignee == "debugger"
    assert not _events(conn, tid, review_routing.RETURNED_EVENT)
    assert _tick(conn, spawn, spawned) is None


def _operator_unblock_reblock(conn, tid):
    assert kb.unblock_task(conn, tid)
    assert kb.block_task(conn, tid, reason="Operator: hold for inspection", kind="dependency")


def _operator_reassign_round_trip(conn, tid):
    assert kb.assign_task(conn, tid, "operator")
    assert kb.assign_task(conn, tid, "debugger")


@pytest.mark.parametrize("operator_move", [_operator_unblock_reblock, _operator_reassign_round_trip])
def test_operator_routing_between_scan_and_hand_back_wins(board, monkeypatch, operator_move):
    """Operator acts after the unlocked scan picked the card but before the
    hand-back txn. The row ends where it started (``blocked``/``debugger``),
    so only the in-txn event recheck can see it."""
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    original = review_routing._hand_back

    def interleaved(*args):
        operator_move(conn, tid)
        assert (_task(conn, tid).status, _task(conn, tid).assignee) == ("blocked", "debugger")
        return original(*args)

    monkeypatch.setattr(review_routing, "_hand_back", interleaved)
    assert _tick(conn, spawn, spawned) is None
    t = _task(conn, tid)
    assert (t.status, t.assignee) == ("blocked", "debugger")
    assert not _events(conn, tid, review_routing.RETURNED_EVENT)


def test_escalation_without_return_to_is_left_alone(board):
    """Escalations recorded before this change carry no ``return_to``."""
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    conn.execute(
        "UPDATE task_events SET payload = json_remove(payload, '$.return_to') "
        "WHERE task_id = ? AND kind = 'assigned'", (tid,),
    )
    conn.commit()
    _diagnose_and_block(conn, tid)
    assert _tick(conn, spawn, spawned) is None
    assert _task(conn, tid).assignee == "debugger"


def test_dry_run_reports_hand_back_without_writing(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    assert review_routing.return_diagnosed(conn, dry_run=True) == [tid]
    kbd.dispatch_once(conn, dry_run=True)
    t = _task(conn, tid)
    assert (t.status, t.assignee) == ("blocked", "debugger")
    assert not _events(conn, tid, review_routing.RETURNED_EVENT)


# ------------------------------------------------------------ workspace

def _complete_diagnosis(conn, tid):
    t = _task(conn, tid)
    assert kb.complete_task(conn, tid, summary=f"Diagnosis only. {DIAGNOSIS}",
                            expected_run_id=t.current_run_id)


def test_scratch_workspace_survives_diagnosis_completion(board):
    """Upstream complete_task rmtree's a scratch workspace; the hop must not."""
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    t = _task(conn, tid)
    ws = Path(t.workspace_path)
    assert kbw._is_managed_scratch_path(ws)  # a real reapable scratch dir
    (ws / "implementer_work.py").write_text("x = 1\n", encoding="utf-8")
    pin = _events(conn, tid, "assigned")[-1]
    assert (t.workspace_kind, pin[review_routing.PIN_KEY]) == ("dir", "scratch")

    _complete_diagnosis(conn, tid)
    assert (ws / "implementer_work.py").read_text(encoding="utf-8") == "x = 1\n"

    assert _tick(conn, spawn, spawned) == "coder"
    t = _task(conn, tid)
    assert (t.workspace_kind, t.workspace_path) == ("scratch", str(ws))  # resolved at spawn
    assert (ws / "implementer_work.py").read_text(encoding="utf-8") == "x = 1\n"


def _git(*args):
    res = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_clean_pushed_worktree_survives_diagnosis_completion(board, tmp_path):
    """Upstream removes a clean, fully pushed worktree (and its wt/ branch)."""
    conn, cfg, spawned, spawn = board
    origin, repo = tmp_path / "origin.git", tmp_path / "project"
    _git("init", "--bare", str(origin))
    _git("clone", str(origin), str(repo))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git("-C", str(repo), "config", k, v)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(repo), "add", "README.md")
    _git("-C", str(repo), "commit", "-m", "init")
    _git("-C", str(repo), "push", "origin", "HEAD")

    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=4)
    tid = kb.create_task(conn, title="card", assignee="coder",
                         workspace_kind="worktree", workspace_path=str(repo))
    assert _tick(conn, spawn, spawned) == "coder"
    wt = Path(_task(conn, tid).workspace_path)
    branch = _task(conn, tid).branch_name
    (wt / "fix.py").write_text("fixed = True\n", encoding="utf-8")
    _git("-C", str(wt), "add", "fix.py")
    _git("-C", str(wt), "commit", "-m", "wip")
    _git("-C", str(wt), "push", "origin", f"HEAD:{branch}")  # clean + pushed: reapable
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"
    assert _task(conn, tid).workspace_path == str(wt)

    _complete_diagnosis(conn, tid)
    assert (wt / "fix.py").exists()
    assert _git("-C", str(repo), "branch", "--list", branch).strip()

    assert _tick(conn, spawn, spawned) == "coder"
    t = _task(conn, tid)
    assert (t.workspace_kind, t.workspace_path, t.branch_name) == ("worktree", str(wt), branch)
    assert (wt / "fix.py").read_text(encoding="utf-8") == "fixed = True\n"


def test_operator_reassign_restores_pinned_workspace_kind(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned)
    _diagnose_and_block(conn, tid)
    assert kb.assign_task(conn, tid, "operator")
    _tick(conn, spawn, spawned)
    assert _task(conn, tid).workspace_kind == "scratch"
    assert _events(conn, tid, review_routing.UNPINNED_EVENT) == [{"workspace_kind": "scratch"}]


def test_genuine_dir_task_is_never_repinned_or_rewritten(board, tmp_path):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=4)
    d = tmp_path / "shared"
    tid = kb.create_task(conn, title="card", assignee="coder", workspace_kind="dir",
                         workspace_path=str(d))
    assert _tick(conn, spawn, spawned) == "coder"
    _review_and_reject(conn, tid, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"
    assert review_routing.PIN_KEY not in _events(conn, tid, "assigned")[-1]
    _complete_diagnosis(conn, tid)
    assert _tick(conn, spawn, spawned) == "coder"
    assert _task(conn, tid).workspace_kind == "dir"


# --------------------------------------------------------------- rounds/cap

def test_next_rejection_after_hand_back_escalates_again_under_cap(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned, cap=4)
    _diagnose_and_block(conn, tid)
    assert _tick(conn, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"   # round 3: new hop
    assert review_routing._loop_state(conn, tid)[0] == 3
    assert _events(conn, tid, "assigned")[-1]["changes_rounds"] == 3


def test_cap_path_hand_back_then_blocks_needs_input_at_cap(board):
    conn, cfg, spawned, spawn = board
    tid = _escalated(conn, cfg, spawn, spawned, cap=2)                   # round 2 == cap
    assert _events(conn, tid, "review_cap_escalated")[0]["previous_assignee"] == "coder"
    _diagnose_and_block(conn, tid)
    assert _tick(conn, spawn, spawned) == "coder"                         # terminal round, fixed by coder
    assert review_routing._loop_state(conn, tid)[0] == 2

    assert _review_and_reject(conn, tid, spawn, spawned) is None         # round 3 -> cap block
    t = _task(conn, tid)
    assert (t.status, t.block_kind, t.assignee) == ("blocked", "needs_input", "coder")
    reason = _events(conn, tid, "blocked")[-1]["reason"]
    assert "kanban.max_review_rounds=2 after the terminal escalated round" in reason
    assert len(_events(conn, tid, "review_cap_escalated")) == 1
    assert _tick(conn, spawn, spawned) is None                           # sticky
