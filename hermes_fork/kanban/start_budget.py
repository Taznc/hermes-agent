"""Rolling per-board worker start budget at dispatch admission (tier T1, see FORK.md).

Upstream ``next`` caps how many workers run at once (``max_spawn``,
``max_in_progress``, ``max_in_progress_per_profile``) but not how many it
STARTS over time. A card that crashes, completes, or gets reclaimed quickly
frees its slot, and the next tick fills it again, so a bad batch can burn
dozens of worker sessions (and their provider quota) in minutes while never
exceeding the concurrency cap. This module adds the missing rate limit:

* ``kanban.dispatch_start_budget``: at most this many worker starts per board
  within the rolling window. Unset, ``0``, negative or non-integer = off, so
  without config upstream behaviour is unchanged.
* ``kanban.dispatch_start_window_seconds``: the window, default 600.

A "start" is a durable ``spawned`` task event (upstream ``_set_worker_pid``
writes it inside the dispatch tick lock), so the count survives restarts and is
shared by every dispatcher on the board: the embedded gateway loop and
``hermes kanban dispatch`` alike. The window is inclusive
(``created_at >= now - window``). Nothing sticky is persisted: every tick
recomputes from the events, so dispatch resumes on its own on the first tick
after ``next_eligible_at``; no ``--resume`` command exists or is needed.

Called from exactly one site: the ``start-budget`` FORK ANCHOR in
``hermes_cli.kanban_db_dispatch._dispatch_once_locked``, right after
upstream's ``_tick_spawn_budget`` and before any row is claimed. It runs under
upstream's per-board ``_dispatch_tick_lock``, so a contended dispatcher never
reaches it (``skipped_locked``) and two dispatchers cannot both spend the last
slot. It only NARROWS the tick's spawn budget; ``max_in_progress`` and the
other concurrency caps are applied independently by upstream first.

Held rows get one ``start_budget_deferred`` event per (task, cooldown), deduped
on ``next_eligible_at`` so a tick loop does not write an event every interval,
and are reported in ``DispatchResult.respawn_guarded`` as ``start_budget`` so
the dispatcher-stuck warning and ``hermes kanban dispatch`` name the reason.

Re-implemented from the frozen ``dev`` branch (``hermes_fork/kanban/
config_defaults.py`` ``dispatch_start_budget`` and ``dispatch_concurrency.py``
``_recent_dispatch_start_window``, fee0106a45), minus dev's JSON pause
sentinel, which next has no reader for.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

DEFAULT_WINDOW_SECONDS = 600
REASON = "start_budget"
EVENT_KIND = "start_budget_deferred"


def settings() -> tuple[Optional[int], int]:
    """``(budget, window_seconds)`` read live from ``kanban.*``; budget None = off."""
    try:
        from hermes_cli.config import load_config

        cfg = (load_config() or {}).get("kanban") or {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    budget = _positive(cfg.get("dispatch_start_budget"))
    window = _positive(cfg.get("dispatch_start_window_seconds")) or DEFAULT_WINDOW_SECONDS
    return budget, window


def _positive(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def recent_starts(
    conn: sqlite3.Connection, *, window_seconds: int, budget: int, now: int,
) -> tuple[int, Optional[int]]:
    """Starts in the inclusive window, and when the next start becomes legal.

    ``next_eligible_at`` is None while under budget. At or over budget it is
    the moment the ``(starts - budget)``-th oldest in-window start leaves the
    window, not always the oldest: a hot-lowered budget may need several starts
    to age out before one more is allowed.
    """
    cutoff = now - window_seconds
    starts = int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'spawned' AND created_at >= ?",
        (cutoff,),
    ).fetchone()[0])
    if starts < budget:
        return starts, None
    row = conn.execute(
        "SELECT created_at FROM task_events WHERE kind = 'spawned' AND created_at >= ? "
        "ORDER BY created_at, id LIMIT 1 OFFSET ?",
        (cutoff, starts - budget),
    ).fetchone()
    # Inclusive cutoff: the start is still counted at exactly +window.
    return starts, int(row[0]) + window_seconds + 1


def admit(
    conn: sqlite3.Connection,
    result: Any,
    may_spawn: bool,
    spawn_budget: Optional[int],
    *,
    dry_run: bool,
) -> tuple[bool, Optional[int]]:
    """Narrow upstream's ``(may_spawn, spawn_budget)`` to the rolling start budget.

    Returns the pair unchanged when the budget is off or upstream already
    refused the tick. With ``k`` starts left in the window the tick may spawn
    at most ``k`` (shared by the ready and review lanes); with none left it
    returns ``(False, None)`` so nothing is claimed.
    """
    budget, window = settings()
    if budget is None or not may_spawn:
        return may_spawn, spawn_budget
    now = int(time.time())
    starts, next_eligible_at = recent_starts(conn, window_seconds=window, budget=budget, now=now)
    remaining = budget - starts
    if remaining > 0:
        return True, remaining if spawn_budget is None else min(spawn_budget, remaining)
    payload = {
        "reason": "start_budget_exceeded",
        "recent_starts": starts,
        "budget": budget,
        "window_seconds": window,
        "next_eligible_at": next_eligible_at,
    }
    _record_held(conn, result, payload, dry_run=dry_run)
    return False, None


def _held_rows(conn: sqlite3.Connection) -> list[str]:
    """Assigned, unclaimed ready/review rows the tick would otherwise consider."""
    from hermes_cli import kanban_db_dispatch as _kbd

    lanes = ["ready"] + (["review"] if _kbd.review_dispatch_enabled() else [])
    ids: list[str] = []
    for lane in lanes:
        ids.extend(row["id"] for row in _kbd._lane_rows(conn, lane) if row["assignee"])
    return ids


def _already_recorded(conn: sqlite3.Connection, task_id: str, next_eligible_at: Any) -> bool:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, EVENT_KIND),
    ).fetchone()
    if row is None:
        return False
    try:
        return json.loads(row[0] or "{}").get("next_eligible_at") == next_eligible_at
    except (TypeError, ValueError):
        return False


def _record_held(conn: sqlite3.Connection, result: Any, payload: dict, *, dry_run: bool) -> None:
    from hermes_cli import kanban_db as _kb

    held = _held_rows(conn)
    for task_id in held:
        result.respawn_guarded.append((task_id, REASON))
    if dry_run or not held:
        return
    fresh = [tid for tid in held if not _already_recorded(conn, tid, payload["next_eligible_at"])]
    if not fresh:
        return
    with _kb.write_txn(conn):
        for task_id in fresh:
            _kb._append_event(conn, task_id, EVENT_KIND, payload)
    _kb._log.info(
        "kanban dispatch: start budget %d/%ds used (%d starts); holding %d card(s) until %s",
        payload["budget"], payload["window_seconds"], payload["recent_starts"],
        len(held), payload["next_eligible_at"],
    )
