"""Fork-owned review routing at dispatch admission (tier T1, see FORK.md).

Upstream ``next`` has two review-loop gaps this module closes:

* A first ``request_review`` without ``reviewer=`` leaves the card on its
  implementer, so the review lane spawns the implementer to review its own
  work. ``kanban.default_reviewer`` reassigns such a row before it is claimed.
* ``request_changes`` hands the card back to the implementer unconditionally,
  so review <-> rework can loop forever. After ``ESCALATE_AFTER_CHANGES``
  returns the rework goes to ``kanban.review_rework_escalation_profile``; at
  ``kanban.max_review_rounds`` the card gets ONE terminal escalated round (a
  durable ``review_cap_escalated`` event) and blocks ``needs_input`` when that
  round also comes back, or immediately when no escalation profile is set.

The escalation is a DIAGNOSIS HOP, not a dead end. The escalation ``assigned``
event records ``return_to`` (the implementer) and the dispatcher leaves the
escalated worker a comment with the hand-back protocol. When that worker ends
its run WITHOUT handing in for review (a ``kanban_block`` of any kind except
``needs_input``, or a ``kanban_complete``), :func:`return_diagnosed` gives the
card back to ``return_to`` in ``ready`` on the next tick: no operator action.
The diagnosis (its comments and the run summary) stays on the card and so
reaches the implementer's worker context. The hop is not a review round: it
writes no ``changes_requested``, a diagnosis ``completed`` does not reset the
round count, and a ``review_rework_returned`` marker newer than the latest
``changes_requested`` stops the same round from escalating twice. An escalated
worker that DOES request review is acting as the implementer (a fixing
escalation profile) and is never handed back. ``needs_input`` stays blocked for
a human. Escalations written before ``return_to`` existed are left alone.

Every decision is derived from durable ``task_events`` (``changes_requested``
since the last ``completed``, ``review_requested`` provenance, ``assigned``
sources), never from in-memory tick state, so it survives dispatcher restarts
and multiple gateways. An operator's ``assign``/``reassign`` or ``unblock``
newer than the latest ``changes_requested`` is an explicit routing decision
and always wins. Writes made here carry ``source: kanban.*`` so they are
never mistaken for operator intent.

:func:`admit` is called from exactly one site: the ``review-routing`` FORK
ANCHOR at the top of ``hermes_cli.kanban_db_dispatch._dispatch_lane_task``,
BEFORE the claim and spawn, so a refused row never starts a worker. A blocked
or done card is never a lane row, so :func:`return_diagnosed` rides the fork's
only per-tick pre-lane seam instead: the ``start-budget`` anchor
(``hermes_fork.kanban.start_budget.admit``), inside the per-board tick lock,
after reclaim/promotion and before lanes are enumerated, so a returned card
dispatches in the same tick and no new upstream anchor is needed. All settings
default off, so
without config upstream behaviour is unchanged. Ported from the frozen
``dev`` branch (``kanban_db_dispatch.py`` ``_changes_requested_state`` /
``_apply_rework_escalation`` / ``_apply_review_round_cap`` /
``_apply_default_reviewer``, c2276ae2ea), re-implemented against next's seams.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

ESCALATE_AFTER_CHANGES = 2
"""``changes_requested`` returns before rework leaves the implementer."""

_SRC_REVIEWER = "kanban.default_reviewer"
_SRC_ESCALATION = "kanban.review_rework_escalation_profile"
_SRC_RETURN = "kanban.review_rework_return"

RETURNED_EVENT = "review_rework_returned"
"""Durable marker: the escalated diagnosis hop ended and the card went home."""

COMMENT_AUTHOR = "dispatcher"


def admit(
    conn: sqlite3.Connection, row: Any, assignee: str, *, lane: str, dry_run: bool,
) -> str:
    """Profile to dispatch ``row`` under, or ``""`` to skip it this tick.

    ``""`` means the card was parked (blocked at the review-round cap) or a
    guarded write lost a race; either way no worker must start.
    """
    if lane == "review":
        return _route_review(conn, row["id"], assignee, dry_run=dry_run)
    if lane == "ready":
        return _route_rework(conn, row["id"], assignee, dry_run=dry_run)
    return assignee


# ---------------------------------------------------------------- settings

def _settings() -> dict:
    try:
        from hermes_cli.config import load_config

        cfg = (load_config() or {}).get("kanban") or {}
    except Exception:
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _profile(value: Any) -> Optional[str]:
    """A configured profile name that this home may actually spawn.

    Fails CLOSED: these settings OVERWRITE a real assignee, so an unverifiable
    name (profiles module unimportable, typo, outside ``dispatch_profiles``)
    resolves to ``None`` and the card keeps its current owner.
    """
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        return None
    from hermes_cli import kanban_db_dispatch as _kbd

    exists = _kbd._profile_exists_fn()
    return name if exists is not None and exists(name) else None


def _max_rounds(value: Any) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return 0
    return rounds if rounds > 0 else 0


# ------------------------------------------------------------ event reads

def _payload(raw: Any) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _loop_reset_id(conn: sqlite3.Connection, task_id: str) -> int:
    """Id of the newest ``completed`` that ends a review loop, else 0.

    A diagnosis hop that ended with ``kanban_complete`` is not an approval:
    the ``review_rework_returned`` marker names that ``completed`` event, and
    it is skipped so the hop never resets the round count."""
    diagnosis_completions = set()
    for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, RETURNED_EVENT),
    ):
        data = _payload(r["payload"])
        if data.get("terminal") == "completed" and isinstance(data.get("diagnosis_event"), int):
            diagnosis_completions.add(data["diagnosis_event"])
    for r in conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC",
        (task_id,),
    ):
        if int(r["id"]) not in diagnosis_completions:
            return int(r["id"])
    return 0


def _loop_state(
    conn: sqlite3.Connection, task_id: str,
) -> tuple[int, Optional[int], Optional[int]]:
    """``(rounds, latest_changes_id, latest_cap_marker_id)`` since the last
    approval ``completed`` (a reopened done card starts a fresh loop).
    ``rounds`` counts ``changes_requested``; the marker is
    ``review_cap_escalated``."""
    row = conn.execute(
        "SELECT SUM(kind = 'changes_requested') AS n, "
        "MAX(CASE WHEN kind = 'changes_requested' THEN id END) AS latest, "
        "MAX(CASE WHEN kind = 'review_cap_escalated' THEN id END) AS marker "
        "FROM task_events WHERE task_id = ? "
        "AND kind IN ('changes_requested', 'review_cap_escalated') AND id > ?",
        (task_id, _loop_reset_id(conn, task_id)),
    ).fetchone()
    return (
        int(row["n"] or 0),
        int(row["latest"]) if row["latest"] is not None else None,
        int(row["marker"]) if row["marker"] is not None else None,
    )


def _operator_routed_after(conn: sqlite3.Connection, task_id: str, event_id: int) -> bool:
    """An operator ``assigned`` (no ``kanban.*`` source) or ``unblocked`` event
    newer than ``event_id``: explicit human routing that overrides policy."""
    for ev in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind IN ('assigned', 'unblocked')",
        (task_id, event_id),
    ):
        if ev["kind"] == "unblocked":
            return True
        if not str(_payload(ev["payload"]).get("source") or "").startswith("kanban."):
            return True
    return False


def _implementer_owned(conn: sqlite3.Connection, task_id: str, assignee: str) -> bool:
    """The review row is still owned by the profile that implemented it and
    no reviewer was ever routed for it. Unknown provenance fails closed."""
    ev = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if ev is None:
        return False
    data = _payload(ev["payload"])
    reviewer = data.get("reviewer")
    if isinstance(reviewer, str) and reviewer.strip():
        return False
    if data.get("implementer") != assignee:
        return False
    return not _operator_routed_after(conn, task_id, int(ev["id"]))


# ----------------------------------------------------------------- writes

def _reassign(
    conn: sqlite3.Connection, task_id: str, *, status: str, previous: str, new: str,
    payload: dict, extra_event: Optional[tuple[str, dict]] = None,
    comment: Optional[str] = None,
) -> bool:
    """Guarded handoff: only an unclaimed row still in ``status`` and still
    owned by ``previous`` moves, so a race is a no-op with no phantom event.
    ``from`` is recorded so the respawn guard sees a real handoff. With
    ``new == previous`` only ``extra_event`` is written (same guard).
    ``comment`` lands in the same txn, before the worker is spawned, so the
    new owner's worker context carries it."""
    from hermes_cli import kanban_db as _kb

    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET assignee = ? "
            "WHERE id = ? AND status = ? AND claim_lock IS NULL AND assignee = ?",
            (new, task_id, status, previous),
        )
        if cur.rowcount != 1:
            return False
        if new != previous:
            _kb._append_event(
                conn, task_id, "assigned", {"assignee": new, "from": previous, **payload},
            )
        if extra_event is not None:
            _kb._append_event(conn, task_id, extra_event[0], extra_event[1])
        if comment:
            _kb.add_comment(conn, task_id, COMMENT_AUTHOR, comment)
    if new != previous:
        _kb.notify_task_updated(conn, task_id, ("assignee",))
    return True


def _last_reason(conn: sqlite3.Connection, event_id: int) -> str:
    row = conn.execute("SELECT payload FROM task_events WHERE id = ?", (event_id,)).fetchone()
    reason = _payload(row["payload"] if row else None).get("reason")
    return reason.strip()[:300] if isinstance(reason, str) else ""


# --------------------------------------------------------------- routing

def _route_review(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> str:
    reviewer = _profile(_settings().get("default_reviewer"))
    if not reviewer or reviewer == assignee or not _implementer_owned(conn, task_id, assignee):
        return assignee
    if dry_run:
        return reviewer
    moved = _reassign(
        conn, task_id, status="review", previous=assignee, new=reviewer,
        payload={"source": _SRC_REVIEWER},
    )
    return reviewer if moved else ""


def _route_rework(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> str:
    rounds, latest, cap_marker = _loop_state(conn, task_id)
    if not rounds or latest is None or _operator_routed_after(conn, task_id, latest):
        return assignee
    if _returned_after(conn, task_id, latest):
        return assignee  # this round already had its diagnosis hop; the fix is home
    cfg = _settings()
    cap = _max_rounds(cfg.get("max_review_rounds"))
    escalation = _profile(cfg.get("review_rework_escalation_profile"))
    if not cap or rounds < cap:
        if escalation is None or escalation == assignee or rounds < ESCALATE_AFTER_CHANGES:
            return assignee
        return _escalate(conn, task_id, assignee, escalation, rounds, cap=None, dry_run=dry_run)
    if cap_marker is not None and cap_marker > latest:
        return assignee  # terminal round granted, worker not started yet
    if escalation is not None and cap_marker is None:
        return _escalate(conn, task_id, assignee, escalation, rounds, cap=cap, dry_run=dry_run)
    if not dry_run:
        from hermes_cli import kanban_db as _kb

        last = _last_reason(conn, latest)
        _kb.block_task(
            conn, task_id, kind="needs_input",
            reason=(
                f"review round cap: {rounds} changes_requested round(s) reached "
                f"kanban.max_review_rounds={cap}"
                + (" after the terminal escalated round" if cap_marker is not None else "")
                + ". Reassign or unblock to grant another round."
                + (f" Last reviewer reason: {last}" if last else "")
            ),
        )
    return ""


def _escalate(
    conn: sqlite3.Connection, task_id: str, assignee: str, escalation: str, rounds: int,
    *, cap: Optional[int], dry_run: bool,
) -> str:
    if dry_run:
        return escalation
    extra = None
    if cap is not None:
        extra = ("review_cap_escalated", {
            "changes_rounds": rounds, "max_review_rounds": cap,
            "escalation_profile": escalation, "previous_assignee": assignee,
        })
    moved = _reassign(
        conn, task_id, status="ready", previous=assignee, new=escalation,
        payload={"source": _SRC_ESCALATION, "changes_rounds": rounds, "return_to": assignee},
        extra_event=extra,
        comment=escalation_brief(assignee, escalation, rounds) if escalation != assignee else None,
    )
    return escalation if moved else ""


def escalation_brief(implementer: str, escalation: str, rounds: int) -> str:
    """The hand-back protocol, left as a comment so it is in the escalated
    worker's context (newest comment at spawn time)."""
    return (
        f"Review escalation: {rounds} review round(s) returned changes on @{implementer}'s "
        f"work, so this rework was routed to @{escalation} "
        "(kanban.review_rework_escalation_profile).\n"
        "If you fix it yourself, finish with kanban_request_review as usual.\n"
        "If your role is to diagnose, or you cannot fix it: post the root cause, the "
        "reproduction and the recommended fix as a kanban_comment, then end the run with "
        "kanban_block(kind=\"transient\", reason=\"ACTION: hand back to "
        f"@{implementer}: <one-line diagnosis>\"). The "
        f"dispatcher hands the card back to @{implementer} on its next tick with your "
        "comments in context; this hop does not count as a review round. Do NOT use "
        "kind=\"capability\" for this, and do not kanban_complete (that marks the card done "
        "and releases its children until the hand-back reopens it). Use kind=\"needs_input\" "
        "only when a human decision is required: that one stays blocked for the operator."
    )


# --------------------------------------------------------------- hand-back

_HANDBACK_TERMINALS = (
    "blocked", "block_loop_detected", "completed", "review_requested", "unblocked", "gave_up",
)
_TERMINAL_STATUS = {"blocked": "blocked", "block_loop_detected": "triage", "completed": "done"}
"""Hand-back terminal event -> the status it leaves the card in. A second
same-kind block routes to ``triage`` upstream (``block_loop_detected``), which
is exactly where t_66deb47b stalled; it is a hand-back too."""
_DONE_LOOKBACK_SECONDS = 7 * 24 * 3600
"""A diagnosis ``completed`` older than this is left alone (bounds the scan)."""


def _returned_after(conn: sqlite3.Connection, task_id: str, event_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND id > ? LIMIT 1",
        (task_id, RETURNED_EVENT, event_id),
    ).fetchone() is not None


def _escalation_owner(conn: sqlite3.Connection, task_id: str, assignee: str) -> Optional[tuple[int, dict]]:
    """``(event_id, payload)`` of the newest ``assigned`` event when it is an
    escalation with a hand-back target and the card still sits with it."""
    ev = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'assigned' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if ev is None:
        return None
    data = _payload(ev["payload"])
    target = data.get("return_to")
    if (
        data.get("source") != _SRC_ESCALATION
        or data.get("assignee") != assignee
        or not isinstance(target, str) or not target.strip() or target == assignee
    ):
        return None
    return int(ev["id"]), data


def _diagnosis_terminal(
    conn: sqlite3.Connection, task_id: str, since: int, owner: str,
) -> Optional[sqlite3.Row]:
    """The escalated worker's run-ending event after ``since``, when it is a
    hand-back: a block (any kind but ``needs_input``) or a ``completed``,
    from a run ``owner`` CLAIMED. Anything else (a review handoff, an operator
    unblock/close, a breaker trip) is not a hand-back and returns None."""
    ev = conn.execute(
        f"SELECT id, kind, run_id, payload FROM task_events WHERE task_id = ? AND id > ? "
        f"AND kind IN ({','.join('?' * len(_HANDBACK_TERMINALS))}) ORDER BY id DESC LIMIT 1",
        (task_id, since, *_HANDBACK_TERMINALS),
    ).fetchone()
    if ev is None or ev["kind"] not in _TERMINAL_STATUS or ev["run_id"] is None:
        return None
    if ev["kind"] != "completed" and _payload(ev["payload"]).get("kind") == "needs_input":
        return None
    # A worker's run was claimed (claim_lock set) under its profile; an
    # operator's CLI block or complete on an unclaimed card synthesizes a run
    # without a claim.
    run = conn.execute(
        "SELECT profile, claim_lock FROM task_runs WHERE id = ? AND task_id = ?",
        (ev["run_id"], task_id),
    ).fetchone()
    if run is None or not run["claim_lock"] or run["profile"] != owner:
        return None
    return ev


def return_diagnosed(conn: sqlite3.Connection, *, dry_run: bool) -> list[str]:
    """Hand every finished escalation hop back to its implementer.

    Runs once per dispatch tick (see module docstring for the call site).
    A card qualifies when its newest ``assigned`` event is a
    ``kanban.review_rework_escalation_profile`` escalation carrying
    ``return_to``, it is still owned by that profile, and that profile's worker
    then ended its run with a hand-back terminal (:func:`_diagnosis_terminal`).
    The card is reassigned to ``return_to`` and lands in ``ready`` (``todo``
    while a parent is open). A done card is reopened through upstream's
    descendant invalidation, so children released by the diagnosis
    ``completed`` are re-gated. Returns the task ids handed back.
    """
    import time

    from hermes_cli import kanban_db as _kb

    rows = conn.execute(
        "SELECT id, status, assignee FROM tasks WHERE assignee IS NOT NULL AND claim_lock IS NULL "
        "AND current_run_id IS NULL "
        "AND (status IN ('blocked', 'triage') OR (status = 'done' AND completed_at >= ?))",
        (int(time.time()) - _DONE_LOOKBACK_SECONDS,),
    ).fetchall()
    returned: list[str] = []
    for row in rows:
        task_id, status, owner = row["id"], row["status"], row["assignee"]
        esc = _escalation_owner(conn, task_id, owner)
        if esc is None:
            continue
        esc_id, esc_data = esc
        terminal = _diagnosis_terminal(conn, task_id, esc_id, owner)
        if terminal is None or _TERMINAL_STATUS[terminal["kind"]] != status:
            continue
        if dry_run:
            returned.append(task_id)
            continue
        if _hand_back(conn, _kb, task_id, status, owner, esc_data, terminal):
            returned.append(task_id)
    return returned


def _hand_back(
    conn: sqlite3.Connection, _kb: Any, task_id: str, status: str, owner: str,
    esc_data: dict, terminal: sqlite3.Row,
) -> bool:
    target = esc_data["return_to"]
    terminal_payload = _payload(terminal["payload"])
    reason = terminal_payload.get("reason") if terminal["kind"] == "blocked" else terminal_payload.get("summary")
    first = (reason or "").strip().splitlines()[0][:300] if isinstance(reason, str) and reason.strip() else ""
    terminations: list = []
    with _kb.write_txn(conn):
        new_status = _kb._landing_status_after_parents(conn, task_id)
        # block_kind/block_recurrences reset: the hand-back block is the
        # escalation protocol's end-of-step signal, not a wall, so the
        # implementer's first genuine block must not count as its recurrence.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, assignee = ?, completed_at = NULL, "
            "block_kind = NULL, block_recurrences = 0, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status = ? AND assignee = ? AND claim_lock IS NULL "
            "AND current_run_id IS NULL",
            (new_status, target, task_id, status, owner),
        )
        if cur.rowcount != 1:
            return False
        marker = {
            "return_to": target, "from": owner, "terminal": terminal["kind"],
            "diagnosis_event": int(terminal["id"]), "diagnosis_run_id": int(terminal["run_id"]),
            "changes_rounds": esc_data.get("changes_rounds"), "status": new_status,
        }
        if terminal["kind"] == "blocked":
            marker["block_kind"] = terminal_payload.get("kind")
        _kb._append_event(conn, task_id, RETURNED_EVENT, marker)
        # ``from`` + a different assignee: the respawn guard reads this as a
        # real handoff (lifts ``active_pr`` for the implementer's own PR).
        _kb._append_event(conn, task_id, "assigned", {
            "assignee": target, "from": owner, "source": _SRC_RETURN,
        })
        # A requeue event: lifts ``recent_success`` after a diagnosis
        # ``completed`` and shows the move in the live feed.
        _kb._append_event(conn, task_id, "status", {
            "status": new_status, "reason": RETURNED_EVENT, "previous_status": status,
        })
        if status == "done":
            terminations = _kb.invalidate_descendants_for_parent_reopen(
                conn, task_id, author=COMMENT_AUTHOR,
            )["terminations"]
        _kb.add_comment(conn, task_id, COMMENT_AUTHOR, (
            f"Rework handed back to @{target} after @{owner}'s diagnosis "
            f"(run {int(terminal['run_id'])}, {terminal['kind']}"
            + (f": {first}" if first else "")
            + "). Read the diagnosis in the comments and attempt above, fix it, then "
            "request review. This hop was not a review round."
        ))
    for pid, claim_lock, started_at in terminations:
        _kb._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    _kb.notify_task_updated(conn, task_id, ("assignee", "status"))
    _kb._log.info(
        "kanban review rework handed back task=%s from=%s to=%s terminal=%s",
        task_id, owner, target, terminal["kind"],
    )
    return True
