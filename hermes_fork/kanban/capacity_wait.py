"""Healthy capacity wait vs zero-spawn starvation for dispatcher health (tier T1).

The embedded dispatcher's "stuck" streak (``dispatcher_standby._lead``) counts
ticks where spawnable ready/review work exists but nothing spawned. Upstream's
``DispatchResult`` cannot tell why when the fleet is simply full:
``_tick_spawn_budget`` refuses a tick whose concurrency cap (``max_spawn`` per
board, ``max_in_progress`` per host) is already occupied and returns before any
row is looked at, recording nothing. A healthy, fully occupied fleet therefore
accrued "dispatcher stuck ... 0 workers spawned" with ``no_capacity_boards=0``.

A board WAITS ON CAPACITY for a tick when it spawned nothing, has spawnable
work, and every spawnable row was held by a concurrency limit:

* upstream refused the tick on its concurrency cap (:func:`mark`), or
* every spawnable row was deferred by ``max_in_progress_per_profile``
  (``DispatchResult.skipped_per_profile_capped``).

Everything else keeps counting toward the stuck warning, as before: work held
by a guard (respawn guard, provider ``rate_limit_cooldown``, start budget,
board pause), critical memory pressure (a resource alarm, not normal
saturation), and the real starvation case, spawnable work with free capacity
that does not start (a failed spawn leaves no trace on the result, but its row
is back in ``ready`` and is not in the per-profile-capped set). A whole tick is
a capacity wait only when no board has spawnable work held for another reason.

Dead or hung workers are not hidden by this: the reclaim phase runs before the
cap check, so a crashed/stale/timed-out worker frees its slot in the same tick
and the board is no longer saturated.

:func:`mark` is called from :func:`hermes_fork.kanban.start_budget.admit`
(the ``start-budget`` FORK ANCHOR), the only fork seam that sees upstream's
``may_spawn`` verdict; :func:`tick_capacity_wait_boards` from
``dispatcher_standby._lead``. No upstream lines.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, Iterable, Optional

ATTR = "fork_capacity_wait"


def mark(result: Any, may_spawn: bool) -> None:
    """Record that upstream refused this tick because its concurrency cap is full.

    ``_tick_spawn_budget`` refuses for exactly two reasons: an occupied cap, or
    critical memory pressure, which it records on ``result.memory_pressure``.
    """
    if not may_spawn and getattr(result, "memory_pressure", None) != "critical":
        setattr(result, ATTR, True)


def _spawnable_ids(conn: sqlite3.Connection) -> set[str]:
    """Unclaimed ready (and, when review dispatch is on, review) rows a profile would run.

    Same predicate as upstream's ``has_spawnable_ready`` / ``has_spawnable_review``
    (the gateway's ready probe), per row instead of per board.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    lanes = ("ready", "review") if kbd.review_dispatch_enabled() else ("ready",)
    rows = conn.execute(
        f"SELECT id, assignee FROM tasks WHERE status IN ({','.join('?' * len(lanes))}) "
        "AND assignee IS NOT NULL AND claim_lock IS NULL",
        lanes,
    ).fetchall()
    profile_exists = kbd._profile_exists_fn()
    return {row[0] for row in rows if profile_exists is None or profile_exists(row[1])}


def board_state(conn: sqlite3.Connection, result: Any) -> Optional[bool]:
    """``True`` = waits on capacity, ``False`` = spawnable work held otherwise, ``None`` = nothing pending."""
    if result is None:
        return False
    pending = _spawnable_ids(conn)
    if getattr(result, "spawned", None) or not pending:
        return None
    if getattr(result, ATTR, False):
        return True
    capped = {task_id for task_id, *_ in getattr(result, "skipped_per_profile_capped", ())}
    return pending <= capped


def tick_capacity_wait_boards(results: Optional[Iterable[tuple[str, Any]]]) -> int:
    """Boards waiting on capacity this tick; ``0`` if any board's pending work is held otherwise."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    waiting = 0
    with kb.pin_first_board_resolution():
        for slug, result in results or ():
            conn = None
            try:
                conn = kbc.connect(board=slug)
                state = board_state(conn, result)
            except Exception:
                return 0  # cannot classify: keep the stuck streak's default accounting
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
            if state is False:
                return 0
            waiting += bool(state)
    return waiting
