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

Every decision is derived from durable ``task_events`` (``changes_requested``
since the last ``completed``, ``review_requested`` provenance, ``assigned``
sources), never from in-memory tick state, so it survives dispatcher restarts
and multiple gateways. An operator's ``assign``/``reassign`` or ``unblock``
newer than the latest ``changes_requested`` is an explicit routing decision
and always wins. Writes made here carry ``source: kanban.*`` so they are
never mistaken for operator intent.

Called from exactly one site: the ``review-routing`` FORK ANCHOR at the top
of ``hermes_cli.kanban_db_dispatch._dispatch_lane_task``, BEFORE the claim and
spawn, so a refused row never starts a worker. All settings default off, so
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


def _loop_state(
    conn: sqlite3.Connection, task_id: str,
) -> tuple[int, Optional[int], Optional[int]]:
    """``(rounds, latest_changes_id, latest_cap_marker_id)`` since the last
    ``completed`` (a reopened done card starts a fresh loop). ``rounds``
    counts ``changes_requested``; the marker is ``review_cap_escalated``."""
    row = conn.execute(
        "SELECT SUM(kind = 'changes_requested') AS n, "
        "MAX(CASE WHEN kind = 'changes_requested' THEN id END) AS latest, "
        "MAX(CASE WHEN kind = 'review_cap_escalated' THEN id END) AS marker "
        "FROM task_events WHERE task_id = ? "
        "AND kind IN ('changes_requested', 'review_cap_escalated') AND id > COALESCE(("
        "  SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'completed'"
        "), 0)",
        (task_id, task_id),
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
) -> bool:
    """Guarded handoff: only an unclaimed row still in ``status`` and still
    owned by ``previous`` moves, so a race is a no-op with no phantom event.
    ``from`` is recorded so the respawn guard sees a real handoff. With
    ``new == previous`` only ``extra_event`` is written (same guard)."""
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
        payload={"source": _SRC_ESCALATION, "changes_rounds": rounds}, extra_event=extra,
    )
    return escalation if moved else ""
