"""Review routing at dispatch admission (``hermes_fork.kanban.review_routing``).

Drives the real ``dispatch_once`` -> ``_dispatch_lane_task`` path on an
isolated HERMES_HOME/kanban.db, so the ``review-routing`` FORK ANCHOR is what
is under test, not the helper in isolation. ``spawn_fn`` records which
profile a worker would have started under; nothing is actually spawned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import review_routing

PROFILES = {"coder", "reviewer", "debugger", "operator"}


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    kanban_cfg: dict = {"review_dispatch": True}
    monkeypatch.setattr(profmod, "profile_exists", lambda name: name in PROFILES)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": kanban_cfg})
    spawned: list[tuple[str, str]] = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee))
        return None

    with kbc.connect() as conn:
        yield conn, kanban_cfg, spawned, spawn


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else None
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (tid, kind),
        )
    ]


def _task(conn, tid):
    return kb.get_task(conn, tid)


def _tick(conn, spawn, spawned):
    """One dispatcher tick; returns the profile spawned for the card (or None)."""
    before = len(spawned)
    kbd.dispatch_once(conn, spawn_fn=spawn)
    return spawned[-1][1] if len(spawned) > before else None


def _start(conn, spawn, spawned, *, assignee="coder"):
    tid = kb.create_task(conn, title="card", assignee=assignee)
    assert _tick(conn, spawn, spawned) == assignee
    return tid


def _hand_in(conn, tid, reviewer=None):
    t = _task(conn, tid)
    assert t.status == "running"
    assert kb.request_review(
        conn, tid, summary="ready", reviewer=reviewer, expected_run_id=t.current_run_id,
    )


def _reject(conn, tid, reason="still wrong"):
    t = _task(conn, tid)
    assert t.status == "running"
    ok, who = kb.request_changes(conn, tid, reason=reason, expected_run_id=t.current_run_id)
    assert ok, who
    return who


def _review_and_reject(conn, tid, spawn, spawned):
    """implementer hands in -> reviewer spawns -> reviewer rejects -> next tick."""
    _hand_in(conn, tid)
    assert _tick(conn, spawn, spawned) == "reviewer"
    _reject(conn, tid)
    return _tick(conn, spawn, spawned)


# ------------------------------------------------------------ initial review

def test_omitted_reviewer_routes_to_default_reviewer_before_spawn(board):
    conn, cfg, spawned, spawn = board
    cfg["default_reviewer"] = "reviewer"
    tid = _start(conn, spawn, spawned)
    _hand_in(conn, tid)  # no reviewer= : upstream leaves the row on "coder"
    assert _task(conn, tid).assignee == "coder"

    assert _tick(conn, spawn, spawned) == "reviewer"
    assert _task(conn, tid).assignee == "reviewer"
    assert _events(conn, tid, "assigned")[-1] == {
        "assignee": "reviewer", "from": "coder", "source": "kanban.default_reviewer",
    }
    # Provenance still names the implementer, so a rejection returns to it.
    assert _reject(conn, tid) == "coder"


def test_without_default_reviewer_upstream_behaviour_is_unchanged(board):
    conn, _cfg, spawned, spawn = board
    tid = _start(conn, spawn, spawned)
    _hand_in(conn, tid)
    assert _tick(conn, spawn, spawned) == "coder"  # the self-review gap, default-off


def test_unspawnable_default_reviewer_fails_closed(board):
    conn, cfg, spawned, spawn = board
    cfg["default_reviewer"] = "no-such-profile"
    tid = _start(conn, spawn, spawned)
    _hand_in(conn, tid)
    assert _tick(conn, spawn, spawned) == "coder"
    assert _task(conn, tid).assignee == "coder"


def test_explicit_reviewer_is_never_overridden(board):
    conn, cfg, spawned, spawn = board
    cfg["default_reviewer"] = "reviewer"
    tid = _start(conn, spawn, spawned)
    _hand_in(conn, tid, reviewer="debugger")
    assert _tick(conn, spawn, spawned) == "debugger"
    assert not [e for e in _events(conn, tid, "assigned") if e.get("source")]


def test_operator_assignment_on_review_row_wins(board):
    conn, cfg, spawned, spawn = board
    cfg["default_reviewer"] = "reviewer"
    tid = _start(conn, spawn, spawned)
    _hand_in(conn, tid)
    assert kb.assign_task(conn, tid, "coder")  # human: "yes, coder reviews this"
    assert _tick(conn, spawn, spawned) == "coder"


def test_dry_run_reports_routing_without_writing(board):
    conn, cfg, spawned, spawn = board
    cfg["default_reviewer"] = "reviewer"
    tid = _start(conn, spawn, spawned)
    _hand_in(conn, tid)
    res = kbd.dispatch_once(conn, dry_run=True)
    assert (tid, "reviewer", "") in res.spawned
    assert _task(conn, tid).assignee == "coder"
    assert not [e for e in _events(conn, tid, "assigned") if e.get("source")]


# ----------------------------------------------------------------- re-review

def test_re_review_goes_to_prior_reviewer_and_first_return_stays_home(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=4)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"  # round 1
    _hand_in(conn, tid)  # re-review, reviewer omitted -> upstream prior reviewer
    assert _task(conn, tid).assignee == "reviewer"
    assert _tick(conn, spawn, spawned) == "reviewer"
    assert kb.complete_task(conn, tid, summary="approved")


# ------------------------------------------------------ escalation and cap

def test_second_return_routes_to_escalation_profile(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=4)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"
    last = _events(conn, tid, "assigned")[-1]
    assert last == {"assignee": "debugger", "from": "coder",
                    "source": "kanban.review_rework_escalation_profile", "changes_rounds": 2}
    # The escalated worker's own rejection returns to it (it is the implementer now).
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"
    assert not _events(conn, tid, "review_cap_escalated")


def test_cap_grants_one_terminal_escalated_round_then_blocks(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=3)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"      # 1
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"   # 2 escalated
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"   # 3 terminal
    assert _events(conn, tid, "review_cap_escalated") == [{
        "changes_rounds": 3, "max_review_rounds": 3,
        "escalation_profile": "debugger", "previous_assignee": "debugger",
    }]
    assert _review_and_reject(conn, tid, spawn, spawned) is None         # 4 -> blocked
    t = _task(conn, tid)
    assert (t.status, t.block_kind) == ("blocked", "needs_input")
    blocked = _events(conn, tid, "blocked")[-1]
    assert "kanban.max_review_rounds=3 after the terminal escalated round" in blocked["reason"]
    assert "still wrong" in blocked["reason"]
    assert _tick(conn, spawn, spawned) is None  # sticky: no respawn


def test_cap_with_implementer_still_owning_hands_terminal_round_to_escalation(board):
    """Cap reached before the under-cap escalation ever fired (cap == 2)."""
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=2)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) == "debugger"
    assert _events(conn, tid, "review_cap_escalated")[0]["previous_assignee"] == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) is None
    assert _task(conn, tid).status == "blocked"


def test_cap_without_escalation_profile_blocks_at_cap(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", max_review_rounds=2)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) is None
    t = _task(conn, tid)
    assert (t.status, t.block_kind, t.assignee) == ("blocked", "needs_input", "coder")
    assert "after the terminal" not in _events(conn, tid, "blocked")[-1]["reason"]


def test_unlimited_rounds_by_default(board):
    conn, cfg, spawned, spawn = board
    cfg["default_reviewer"] = "reviewer"
    tid = _start(conn, spawn, spawned)
    for _ in range(4):
        assert _review_and_reject(conn, tid, spawn, spawned) == "coder"


# ------------------------------------------------------- manual escape hatch

def test_operator_reassignment_after_rejection_beats_escalation_and_cap(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", review_rework_escalation_profile="debugger",
               max_review_rounds=2)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    _hand_in(conn, tid)
    assert _tick(conn, spawn, spawned) == "reviewer"
    _reject(conn, tid)                     # round 2 == cap
    assert kb.assign_task(conn, tid, "operator")
    assert _tick(conn, spawn, spawned) == "operator"
    assert not _events(conn, tid, "review_cap_escalated")


def test_unblock_at_cap_grants_exactly_one_more_round(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", max_review_rounds=2)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) is None
    assert kb.unblock_task(conn, tid)
    assert _tick(conn, spawn, spawned) == "coder"
    assert _review_and_reject(conn, tid, spawn, spawned) is None
    # Second same-kind block after an unblock: upstream's BLOCK_RECURRENCE_LIMIT
    # routes it to triage instead of a plain block. Either way, no worker.
    assert _task(conn, tid).status == "triage"
    assert _tick(conn, spawn, spawned) is None


def test_completed_resets_the_round_count(board):
    conn, cfg, spawned, spawn = board
    cfg.update(default_reviewer="reviewer", max_review_rounds=2)
    tid = _start(conn, spawn, spawned)
    assert _review_and_reject(conn, tid, spawn, spawned) == "coder"
    _hand_in(conn, tid)
    assert _tick(conn, spawn, spawned) == "reviewer"
    assert kb.complete_task(conn, tid, summary="approved")
    rounds, _latest, _marker = review_routing._loop_state(conn, tid)
    assert rounds == 0
