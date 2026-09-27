"""Host-restart worker deaths as bounded infrastructure interruptions (tier T1).

Called from ONE place: the ``infra-interruptions`` FORK ANCHOR in
``hermes_cli.kanban_db_dispatch._reclaim_dead_workers``, after upstream has
established that a host-local ``running`` row's worker is dead and BEFORE it
classifies the death (``_classify_dead_worker``) and books it as a crash. That
booking feeds ``_account_crashes``: a counted failure, and when three or more
rows share the bare ``pid N not alive`` fingerprint in one tick, the systemic
breaker trips each one at ``failure_limit=1`` with a sticky ``gave_up``.

The misclassification this fixes (measured, hermes-fork board, 2026-09-26
00:39:16, first tick after a VM reboot): six cards whose workers died WITH the
previous boot were all booked ``pid N not alive`` and all given up at
``failures=1``; none of them had failed. After a reboot no worker survives and
no process can have reaped one, so the only exit evidence is absence.

Evidence used, and only this: the worker's spawn fingerprint
(``"<instantiation epoch>|<start ticks>"``, written by upstream's
``_set_worker_pid``). A fingerprint whose epoch (``boot_id`` + PID 1 start,
``gateway.drain_control.current_instantiation_epoch``) differs from the current
one proves the machine / container was recreated after the spawn — which is
what killed the worker. Every other case keeps upstream's booking:

* an exit witness exists (the reap registry, or the worker's own exit trailer):
  upstream books it (genuine crash counts; rc 75 quota stays a neutral requeue;
  rc 0 stays a protocol violation);
* same boot, no witness: upstream's counted crash (an external ``kill -9`` or
  OOM kill this boot cannot be told apart from a task that exhausts memory);
* no epoch on either side (legacy / ``unverified`` fingerprints, macOS and
  Windows where ``current_instantiation_epoch`` is ``""``): upstream's crash;
* the dispatcher's own max-runtime kill (``enforce_max_runtime``) and expired
  claims (``release_stale_claims``) never reach this seam: those rows leave
  ``running`` in their own passes, which keep counting;
* the allowance is bounded: after ``MAX_HOST_RESTART_INTERRUPTIONS`` neutral
  interruptions since the task's last completed run, every further one falls
  through to upstream's counted crash, so a card whose run keeps taking the
  host down still reaches the breaker. ``consecutive_failures`` is never
  decremented.

Neutral booking: the run closes ``reclaimed`` (upstream's vocabulary for a run
that ended without a verdict) with ``infrastructure: true`` +
``host_restart_interrupted: true`` metadata, a ``reclaimed`` event carrying
``reason: host_restart_interrupted``, and the task returns to its source
phase without touching ``consecutive_failures`` — the same neutral accounting
upstream already applies to host spawn refusals (``infrastructure=True``) and
quota exits.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db as _kb

# Consecutive neutral host-restart interruptions per task before the next one
# is counted (dev's ``kanban.max_infra_interruptions`` default).
MAX_HOST_RESTART_INTERRUPTIONS = 3

# Closed runs to walk when counting the streak; it caps at a handful anyway.
_STREAK_SCAN_LIMIT = 50

REASON = "host_restart_interrupted"


def _current_epoch() -> str:
    from gateway.drain_control import current_instantiation_epoch
    return current_instantiation_epoch()


def spawn_epoch(fingerprint: Any) -> Optional[str]:
    """Instantiation epoch a spawn fingerprint was taken under; None when it carries none."""
    if not isinstance(fingerprint, str) or "|" not in fingerprint:
        return None
    epoch = fingerprint.rsplit("|", 1)[0].strip()
    return epoch or None


def host_restarted_since(spawned_under: Optional[str], current: Optional[str]) -> bool:
    """True only when both epochs are known and differ (fail safe: no evidence, no claim)."""
    return bool(spawned_under) and bool(current) and spawned_under != current


def host_restart_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Neutral host-restart interruptions since the task's last completed run.

    Only a completed run resets it: a counted crash in between (including a
    host-restart death that fell through to upstream once the cap was reached)
    does not, so the neutral allowance is at most ``MAX_HOST_RESTART_INTERRUPTIONS``
    per success and every later unwitnessed death reaches the breaker.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT ?",
        (task_id, _STREAK_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        if row["outcome"] == "completed":
            break
        if _kb._json_dict(row["metadata"]).get("host_restart_interrupted"):
            streak += 1
    return streak


def _has_exit_witness(pid: int, task_id: str, board: Optional[str]) -> bool:
    from hermes_cli import kanban_db_dispatch as _kd
    if _kd._classify_worker_exit(pid)[0] != "unknown":
        return True
    return _kd._worker_log_exit_code(task_id, board=board) is not None


def book_host_restart(
    conn: sqlite3.Connection, row: Any, sweep: Any, *, board: Optional[str] = None,
) -> bool:
    """Book a dead worker killed by a host restart as a neutral interruption.

    ``row`` is the ``tasks`` row upstream's crash sweep selected; the caller
    holds the sweep's write txn. Returns True when the row was handled here
    (caller ``continue``s), False to let upstream classify and book it.
    """
    spawned_under = spawn_epoch(_kb._row_get(row, "worker_started_at"))
    current = _current_epoch()
    if not host_restarted_since(spawned_under, current):
        return False
    tid, pid, claimer = row["id"], int(row["worker_pid"]), row["claim_lock"]
    if _has_exit_witness(pid, tid, board):
        return False
    streak = host_restart_streak(conn, tid) + 1
    if streak > MAX_HOST_RESTART_INTERRUPTIONS:
        return False

    retry_status = _kb._retry_status_for_run(conn, tid)
    error = (
        f"pid {pid} lost to a host restart (spawned under boot {spawned_under}, now {current}); "
        f"requeued without counting a failure (interruption {streak}/{MAX_HOST_RESTART_INTERRUPTIONS})"
    )
    cur = conn.execute(
        "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
        "worker_pid = NULL, worker_started_at = NULL, last_failure_error = ? "
        "WHERE id = ? AND status = 'running' AND worker_pid = ? AND claim_lock IS ?",
        (retry_status, error[:500], tid, pid, claimer),
    )
    if cur.rowcount != 1:
        return True  # raced: upstream would skip this row too
    meta = {
        "pid": pid,
        "claimer": claimer,
        "infrastructure": True,
        "host_restart_interrupted": True,
        "spawn_epoch": spawned_under,
        "current_epoch": current,
        "host_restart_streak": streak,
        "host_restart_cap": MAX_HOST_RESTART_INTERRUPTIONS,
        "retry_status": retry_status,
    }
    run_id = _kb._end_run(conn, tid, outcome="reclaimed", status="reclaimed", error=error, metadata=meta)
    _kb._append_event(conn, tid, "reclaimed", {"reason": REASON, **meta}, run_id=run_id)
    sweep.exited_hook_payloads.append({
        "task_id": tid,
        "assignee": row["assignee"],
        "run_id": run_id,
        "worker_pid": pid,
        "exit_kind": "unknown",
        "exit_code": None,
        "outcome": "reclaimed",
        "retry_status": retry_status,
    })
    _kb._log.info("kanban: task %s worker pid %s lost to a host restart; requeued (%d/%d)",
                  tid, pid, streak, MAX_HOST_RESTART_INTERRUPTIONS)
    return True
