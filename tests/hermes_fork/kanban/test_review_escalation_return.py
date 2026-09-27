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

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
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
