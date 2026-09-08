"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from datetime import timezone
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Hard stop on the review<->changes_requested loop (kanban.max_review_rounds).
# 0 = unlimited (legacy, pre-cap behavior).
DEFAULT_MAX_REVIEW_ROUNDS = 3


def effective_failure_limit(task_max_retries: Optional[Any], failure_limit: int) -> tuple:
    """Circuit-breaker threshold precedence: a task's own ``max_retries`` wins over the
    dispatcher-level ``failure_limit``. Returns ``(effective_limit, limit_source)`` where
    ``limit_source`` is ``"task"`` or ``"dispatcher"`` — the same value recorded in the
    ``gave_up`` event payload (see ``_record_task_failure``).

    Single source of truth for this precedence: ``kanban_diagnostics._rule_repeated_failures``
    imports this function so the diagnostic's threshold can never drift from the breaker's.
    """
    if task_max_retries is not None:
        return int(task_max_retries), "task"
    return int(failure_limit), "dispatcher"


# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and call kanban_block/kanban_complete before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)/pull/\d+",
    re.IGNORECASE,
)

# Parses an owner/repo out of any git remote URL flavor (https, ssh, git@).
_REMOTE_OWNER_REPO_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _repo_slug_from_remote_url(url: str) -> Optional[str]:
    m = _REMOTE_OWNER_REPO_RE.search((url or "").strip())
    return f"{m.group('owner')}/{m.group('repo')}".lower() if m else None


def _git_remote_repo_slug(repo_path: str, *, timeout: float = 3.0) -> Optional[str]:
    """``owner/repo`` for *repo_path*'s ``origin`` remote, or ``None`` (missing dir,
    not a git repo, no remote, or a slow/failing git call — always fail open)."""
    try:
        if not repo_path or not os.path.isdir(repo_path):
            return None
        out = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return _repo_slug_from_remote_url(out.stdout)


def _task_own_repo_slug(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Best-effort ``owner/repo`` this task's own work targets, or ``None`` when it
    cannot be determined (e.g. a scratch workspace with no code) — callers must
    treat ``None`` as "unknown", never as "no repo, so any PR URL counts".

    Resolution order: the task's linked project's primary folder (first-class,
    survives a scratch/dir workspace), else the task's own ``worktree``
    workspace path. Never raises — a missing/renamed project, an unreadable
    projects.db, or a failing ``git`` call all fall through to ``None``.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path, project_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    project_id = row["project_id"]
    if project_id:
        try:
            from hermes_cli import projects_db as _projects_db
            with _projects_db.connect() as pconn:
                project = _projects_db.get_project(pconn, project_id)
            if project is not None:
                primary = project.primary_path or next(
                    (f.path for f in project.folders if f.is_primary),
                    project.folders[0].path if project.folders else None,
                )
                if primary:
                    slug = _git_remote_repo_slug(primary)
                    if slug:
                        return slug
        except Exception:
            pass
    if row["workspace_kind"] == "worktree" and row["workspace_path"]:
        slug = _git_remote_repo_slug(row["workspace_path"])
        if slug:
            return slug
    return None


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a
    misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids that had ``kanban.default_assignee`` applied this
    tick before spawning, so telemetry/CLI/dashboard can show the dispatcher
    acting on the fallback rule rather than explicit assignments."""
    auto_assigned_reviewer: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, previous_assignee, reviewer)`` triples for review-lane cards
    still owned by their implementer that ``kanban.default_reviewer`` reassigned
    this tick — the auto-review counterpart to ``auto_assigned_default``, so
    telemetry/CLI/dashboard can show the implementer->reviewer handoff."""
    auto_escalated_rework: list[tuple[str, str, str, int]] = field(default_factory=list)
    """``(task_id, previous_assignee, escalation_profile, changes_rounds)`` for
    ready cards routed to a specialist after repeated requested-change cycles."""
    blocked_review_round_cap: list[tuple[str, int]] = field(default_factory=list)
    """``(task_id, changes_rounds)`` for ready cards that hit
    ``kanban.max_review_rounds`` and were blocked (kind ``review_round_cap``)
    instead of being re-dispatched to the implementer or escalation profile.
    The hard stop for the review<->changes_requested loop — checked BEFORE
    ``auto_escalated_rework`` so a card at the cap blocks rather than getting
    one more escalated round."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (own-assignee's own-repo GitHub PR
    URL in a recent comment; scoping rules: :func:`check_respawn_guard`)."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    review_no_verdict: list[str] = field(default_factory=list)
    """Task ids whose claimed reviewer run exited cleanly without a verdict (no approve /
    request-changes / escalate). Neutral audit outcome — no failure counted, no breaker fed — but
    NOT auto-recoverable like a rate-limit requeue: the task lands in ``blocked`` and stays sticky
    until an explicit ``kanban_unblock`` reopens it for another reviewer."""
    serialized_coedit: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, holder_id, path)`` for cards deferred this tick because they
    declare an edit target another running/just-spawned card already owns. The
    card gained a real dependency edge on the holder and sits in ``todo`` until
    it completes — NOT operator-actionable and NOT a failure: it is the board
    serializing a co-edit that prose in two card bodies provably cannot."""
    released_coedit: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, holder_id)`` for serialization edges dropped this tick because
    the holder stalled (``blocked``/``on_hold``) and will not produce the work
    the parked card was waiting for. The edge is a lease, not a dependency —
    without this a card would be held hostage until a human unblocked a
    DIFFERENT card, which is the routing bug the guard exists to remove."""
    interrupted: list[str] = field(default_factory=list)
    """Task ids classified ``infra`` (external SIGTERM/SIGKILL, startup-window dead pid, or
    provider quota signature) this tick and requeued WITHOUT counting a failure. See
    ``kanban.max_infra_interruptions`` — a task that keeps landing here is eventually promoted
    into ``auto_blocked`` instead once its persistent interruption streak exceeds the cap."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    dispatch_paused: Optional[dict[str, Any]] = None
    """Current per-board dispatch stop state. Start-budget records are
    self-expiring cooldowns; integrity/safety records remain sticky until an
    operator explicitly resumes the board."""


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap.
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None).

    A worker launched via ``kanban.worker_launcher`` as a systemd ``--user
    --scope`` (``--scope`` is a transparent exec, not a fork) remains a real,
    direct, waitpid-able child of this process in the common case, so this
    function needs no systemd involvement to classify it with full fidelity.
    The narrow case this genuinely can't resolve — the worker is no longer
    this process's child (e.g. a gateway restart re-adopted the task and
    ``pid`` was never reaped by *this* process) — deliberately returns
    ``"unknown"`` rather than querying ``systemctl --user show`` for a
    fabricated verdict: a prior implementation tried that (``ExecMainCode``/
    ``ExecMainStatus`` are never populated for a ``--scope`` unit — systemd
    adopts, never forks, the target process into it — so the query was
    provably always ``None`` on live systemd 255) and is intentionally not
    reinstated here. The bounded ``"unknown"`` outcome is absorbed by the
    infra-interruption classification's neutral bucket instead (see
    ``kanban.max_infra_interruptions``) rather than invented here.
    """
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    try:
        if os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
            if code == 0:
                return ("clean_exit", 0)
            if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
                return ("rate_limited", code)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def reap_worker_zombies() -> "list[int]":
    """Reap all zombie children without blocking; returns reaped PIDs. No-op on Windows."""
    reaped: "list[int]" = []
    if os.name != "nt":
        try:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                _record_worker_exit(pid, status)
                reaped.append(pid)
        except Exception:
            pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Uses ``gateway.status._pid_exists`` (OpenProcess on Windows, ``os.kill(pid, 0)``
    on POSIX). **DO NOT** call ``os.kill(pid, 0)`` directly on Windows — there
    ``sig=0`` is ``CTRL_C_EVENT`` broadcast to the console group, potentially
    killing unrelated processes.

    Zombies (exited, not yet reaped) still pass the existence check, so a
    worker would look "alive" forever between exit and reap. Linux: peek at
    ``/proc/<pid>/status`` and treat ``State: Z`` as dead; macOS: ask ``ps``
    for the BSD ``stat`` field and treat ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _kb._pid_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    systemd_unit: Optional[str] = None,
    signal_fn=None,
    worker_unit: Optional[str] = None,
    stop_unit_fn=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths.

    When ``worker_unit`` is set (the task was spawned through a
    ``kanban.worker_launcher`` that minted a transient systemd ``--user --scope``
    unit — always carrying the explicit ``.scope`` suffix, see
    ``_worker_launcher_unit_name``), termination goes through
    ``systemctl --user stop <unit>`` (``_stop_systemd_unit`` in
    ``tools.process_registry``, reused not reinvented) INSTEAD of a bare
    ``os.kill`` — a scope may contain double-forked descendants that a
    single-PID signal never reaches, and ``systemd.kill(5)``'s default
    ``KillMode=mixed`` already SIGTERMs then SIGKILLs the whole cgroup for
    us. ``_stop_systemd_unit`` alone is not trusted as proof of termination:
    a "not loaded" response there could mean the unit never existed under
    the queried name (e.g. a caller passed a suffix-less name that silently
    resolved to an unrelated ``.service``) with the real worker still
    alive, so ``info["terminated"]`` additionally requires the corroborating
    ``not _kb._pid_alive(pid)`` check below. The raw-PID path below remains
    exactly as before for the default (``worker_unit`` empty) case.
    """
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
        "systemd_unit": systemd_unit,
        "systemd_unit_stopped": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info["host_local"] = True
    if systemd_unit:
        from tools.process_registry import _stop_systemd_unit

        info["systemd_unit_stopped"] = _stop_systemd_unit(systemd_unit)

    if worker_unit:
        info["worker_unit"] = worker_unit
        info["termination_attempted"] = True
        if stop_unit_fn is None:
            from tools.process_registry import _stop_systemd_unit as stop_unit_fn
        stopped = bool(stop_unit_fn(worker_unit))
        if stopped and not _kb._pid_alive(pid):
            info["terminated"] = True
            return info
        if not stopped:
            # The unit stop itself failed (not merely "not loaded") — no
            # corroborating signal was delivered to the pid, so don't guess.
            info["terminated"] = False
            return info
        # stopped is True (systemd reports the unit stopped OR "not loaded",
        # which _stop_systemd_unit also treats as success) but the pid is
        # still alive: the unit was stale/wrong, so fall through to the raw
        # PID path below instead of leaving a live worker un-reclaimable.

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already gone = successful termination. Leaving terminated=False would
        # make the reclaim guard misread a dead worker as alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    if _poll_worker_exit(pid):
        info["terminated"] = True
        return info
    if _kb._pid_alive(pid):
        if not _sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = not _kb._pid_alive(pid)
    return info


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("termination_attempted")
        and termination.get("host_local")
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.

    ``False`` is deliberately ONE return value for two different situations
    this function cannot itself distinguish: ``task_id`` was never real (a
    typo/hallucinated id), or ``task_id`` WAS real and its row is now gone —
    an orphaned worker, e.g. because ``delete_task`` ran against a live
    ``running`` row before the guard in t_749b0510 existed, or via any other
    path that drops a row out from under its worker. Telling those apart
    needs the CALLER's own identity (only the worker itself knows whether
    ``task_id`` is the task it was spawned for), so that distinction is made
    one layer up, in the ``kanban_heartbeat``/``kanban_complete`` tool
    handlers (``tools/kanban_tools.py:_orphan_or_lifecycle_error``), which
    return a structured ``orphaned: true`` field instead of a plain error
    when the vanished id matches the calling worker's own ``HERMES_KANBAN_TASK``.
    An orphaned worker should treat that field as "stop calling kanban tools
    and end this turn" rather than retrying.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.current_run_id, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        run_id = row["current_run_id"]
        # Persist ownership before the termination helper can stop a service
        # or signal the PID. One task/run/pid intent covers TERM/KILL escalation
        # and survives a dispatcher restart before final accounting.
        _kb.persist_timeout_kill_intent(
            conn, task_id=tid, run_id=run_id, worker_pid=pid,
            signal=int(signal.SIGTERM),
        )
        systemd_unit = (
            f"hermes-worker-kanban-{tid}-run-{run_id}.service"
            if run_id is not None
            else None
        )
        termination = _terminate_reclaimed_worker(
            pid, row["claim_lock"], systemd_unit=systemd_unit, signal_fn=signal_fn
        )
        # Do not release an expired claim while its launcher/service cgroup is
        # still alive: that would run a retry beside the timed-out worker.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, row["claim_lock"], now, termination,
                reason="max_runtime_worker_alive",
            )
            continue

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        # The timed-out run is known exactly, so preservation is gated on it:
        # if a newer run has already claimed the task, this sweep must not
        # snapshot over the live worker's in-flight edits.
        _kb._preserve_task_work(conn, tid, expected_run_id=run_id)
        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                # This tick completed the full lifecycle (signal -> reap ->
                # account) itself, so the intent can be consumed immediately;
                # it only needs to survive when a DIFFERENT process reaps the
                # worker later (handled by _classify_dead_worker instead).
                _kb.consume_timeout_kill_intent(
                    conn, task_id=tid, run_id=row["current_run_id"], worker_pid=pid,
                )
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "retry_status": retry_status,
                }
                payload.update(termination)
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                timed_out.append(tid)
        # Outside the write_txn above because ``_record_task_failure`` opens its
        # own. If the breaker trips this flips the task to ``blocked`` and emits
        # ``gave_up`` on top of the ``timed_out`` already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=error,
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={**termination, "retry_status": retry_status},
            )
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.current_run_id, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue
        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        run_id = row["current_run_id"]
        systemd_unit = (
            f"hermes-worker-kanban-{tid}-run-{run_id}.service"
            if run_id is not None
            else None
        )
        termination = _kb._terminate_reclaimed_worker(
            pid, lock, systemd_unit=systemd_unit, signal_fn=signal_fn
        )

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (retry_status, tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to ``ready``
    with a comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID is deferred so no duplicate spawns beside it.
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        pid = row["worker_pid"]
        if pid and _kb._pid_alive(pid):
            # Never requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._opt_int(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                "(dead/gone worker) — requeued to ready",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# A clean exit gets exactly one recovery run: the next worker sees the durable
# prior-run error and can report work that already completed. A second identical
# clean exit is a reporting gap, not evidence that a third full execution is
# worthwhile, so the dispatcher force-blocks it for an explicit decision.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 2

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` and ``spawn_deferred`` runs are neutral and
    skipped (a quota wall or board-wide launcher outage says nothing about the
    task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome in {"rate_limited", "spawn_deferred"}:
            continue
        if outcome == "crashed" and (
            _kb._json_dict(row["metadata"]).get("protocol_violation")
            or "protocol violation" in (row["error"] or "")
        ):
            streak += 1
            continue
        break
    return streak


# A comment cannot complete a task, but an assignee-authored same-attempt handoff
# should stop a blind rerun. Require both an explicit completion claim and a
# concrete deliverable/review signal; a bare "done" or a comment from another
# actor remains insufficient evidence and receives the single recovery attempt.
_COMPLETION_HANDOFF_RE = re.compile(
    r"\b(?:implementation|work|task)\s+(?:is\s+)?complete(?:d)?\b|"
    r"\bready\s+(?:for|to)\s+review\b|\btests?\s+passed\b",
    re.IGNORECASE,
)
_COMPLETION_DELIVERABLE_RE = re.compile(
    r"\bcommit\s+[0-9a-f]{7,40}\b|\bdiff\b|\b(?:pull request|pr)\b|"
    r"\b(?:focused\s+|regression\s+)?tests?\s+passed\b",
    re.IGNORECASE,
)


def _same_attempt_completion_handoff(
    conn: sqlite3.Connection, task_id: str, *, assignee: Optional[str], started_at: Optional[int],
) -> bool:
    """Whether this worker left credible durable handoff evidence after it began."""
    if not assignee or started_at is None:
        return False
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND author = ? AND created_at >= ? "
        "ORDER BY id DESC",
        (task_id, assignee, int(started_at)),
    ).fetchall()
    return any(
        _COMPLETION_HANDOFF_RE.search(row["body"] or "")
        and _COMPLETION_DELIVERABLE_RE.search(row["body"] or "")
        for row in rows
    )


def finalize_clean_worker_exit_without_report(
    conn: sqlite3.Connection, task_id: str, *, expected_run_id: int,
) -> Optional[str]:
    """Detect an rc=0 Kanban worker missing its lifecycle call before it exits.

    Returns ``"recovery"`` for the one allowed no-evidence retry, ``"evidence"``
    when an assignee handoff is parked for verification, ``"blocked"`` after the
    bounded clean-exit streak, or ``None`` if a terminal lifecycle write already
    won the race / this process no longer owns the run.
    """
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT t.status, t.current_run_id, t.assignee, t.max_retries, t.consecutive_failures, "
            "t.block_kind, t.block_recurrences, r.started_at "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "running"
            or row["current_run_id"] is None
            or int(row["current_run_id"]) != int(expected_run_id)
        ):
            return None

        evidence = _same_attempt_completion_handoff(
            conn, task_id, assignee=row["assignee"], started_at=row["started_at"],
        )
        prior_streak = _protocol_violation_streak(conn, task_id)
        task_override = _kb._row_get(row, "max_retries")
        violation_limit, limit_source = effective_failure_limit(
            task_override, _PROTOCOL_VIOLATION_FAILURE_LIMIT,
        )
        streak = prior_streak + 1
        recovery_reason = (
            "verify/recover prior work: worker exited cleanly without a terminal Kanban call, "
            "but its same-attempt comment contains a completion handoff. Verify the prior work and "
            "report it via kanban_complete, kanban_request_review, or kanban_block; do not rerun it blindly."
        )
        error_text = recovery_reason if evidence else _PROTOCOL_VIOLATION_ERROR
        forced_block = not evidence and streak >= violation_limit
        run_outcome = "blocked" if evidence else "crashed"
        run_id = _kb._end_run(
            conn, task_id, outcome=run_outcome, status=run_outcome, error=error_text,
            metadata={
                "protocol_violation": True,
                "detected_at": "worker_exit_boundary",
                "completion_handoff_evidence": evidence,
            },
        )
        _kb._append_event(
            conn, task_id, "protocol_violation",
            {
                "error": error_text,
                "protocol_violation": True,
                "detected_at": "worker_exit_boundary",
                "completion_handoff_evidence": evidence,
            },
            run_id=run_id,
        )

        if evidence:
            new_status, event_kind, set_sql, params, payload = _kb._route_block(
                "needs_input", recovery_reason, "ready",
                prev_kind=_kb._row_get(row, "block_kind"),
                prev_recurrences=int(_kb._row_get(row, "block_recurrences") or 0),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_unit = NULL, last_failure_error = ?, "
                + set_sql + " WHERE id = ? AND status = 'running' AND current_run_id IS NULL",
                (new_status, error_text[:500], *params, task_id),
            )
            _kb._append_event(conn, task_id, event_kind, payload, run_id=run_id)
            return "evidence"

        if forced_block:
            failures = int(row["consecutive_failures"] or 0) + 1
            conn.execute(
                "UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_unit = NULL, consecutive_failures = ?, last_failure_error = ? "
                "WHERE id = ? AND status = 'running' AND current_run_id IS NULL",
                (failures, error_text[:500], task_id),
            )
            _kb._append_event(
                conn, task_id, "gave_up",
                {
                    "failures": failures,
                    "effective_limit": violation_limit,
                    "limit_source": limit_source,
                    "error": error_text,
                    "trigger_outcome": "crashed",
                    "retry_status": "ready",
                    "force_trip": True,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                    "detected_at": "worker_exit_boundary",
                },
                run_id=run_id,
            )
            return "blocked"

        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_unit = NULL, last_failure_error = ? "
            "WHERE id = ? AND status = 'running' AND current_run_id IS NULL",
            (error_text[:500], task_id),
        )
        return "recovery"


_PROTOCOL_VIOLATION_ERROR = (
    # Fallback reaper diagnosis: a worker subprocess exited 0 while its task is
    # still ``running``. The worker/CLI boundary normally records this earlier,
    # after the stop guard's bounded nudges; this remains for older workers and
    # transient boundary-write failures.
    "worker exited cleanly (rc=0) without calling "
    "kanban_complete or kanban_block — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report the result via kanban_complete; a run that ends "
    "without a terminal kanban call counts as failed no "
    "matter what it did."
)


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    review_no_verdict: bool = False
    infra: bool = False

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history doesn't show a
        # phantom crash for a quota wall; a reviewer no-verdict exit gets its own neutral outcome
        # for the same reason (neither a crash nor a failure); an infra death is recorded as
        # ``interrupted`` so it never shows up as a phantom ``crashed`` run outcome either.
        if self.rate_limited:
            return "rate_limited"
        if self.review_no_verdict:
            return "review_no_verdict"
        if self.infra:
            return "interrupted"
        return "crashed"


def _classify_dead_worker(
    conn: sqlite3.Connection, task_id: str, pid: int, claimer: Optional[str],
    retry_status: str = "ready", *, board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping. ``retry_status`` is the
    run's source phase: a clean exit's handling differs by lane (see the review branch)."""
    kind, code = _classify_worker_exit(pid)
    if kind == "clean_exit" and retry_status == "review":
        # A claimed reviewer run exited cleanly without approving, requesting changes, or
        # escalating. NOT a protocol violation (the implementer case below) and NOT a crash: an
        # idle/no-op reviewer pass is a legitimate outcome that says nothing about the work, so it
        # must never feed a failure counter or breaker. But it must not leave the card
        # auto-claimable next tick either (an idle reviewer would spin forever) — it parks in
        # ``blocked`` (``_has_sticky_block`` treats ``review_no_verdict`` as sticky) until an
        # explicit ``kanban_unblock`` reopens it.
        return _DeadWorker(
            kind, code,
            "reviewer exited cleanly without a verdict (no kanban_complete/kanban_request_changes/"
            "kanban_block call) — parked for an explicit review requeue via kanban_unblock; no "
            "failure counted.",
            "review_no_verdict",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            review_no_verdict=True,
        )
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # EX_TEMPFAIL is already a machine-readable quota outcome. When the
        # current run log also carries the reviewed quota signature/deadline,
        # preserve that parsed payload so both per-board and host circuits can
        # register on the first observation. Missing/malformed deadlines must
        # use the existing bounded interruption accounting; treating every
        # EX_TEMPFAIL as neutral would retry forever without advancing either
        # the interruption streak or the ordinary failure budget.
        run_id = _kb._current_run_id(conn, task_id)
        quota_signal = _kb._detect_quota_exit_signal(
            task_id, run_id=run_id, board=board,
        )
        payload = {"pid": pid, "claimer": claimer, "exit_code": code}
        retry_after = quota_signal.get("retry_after_seconds") if quota_signal else None
        if retry_after is not None:
            payload["reason"] = "quota"
            payload["quota_retry_after_seconds"] = retry_after
            if quota_signal and quota_signal.get("host_circuit_published"):
                payload["host_circuit_published"] = True
            # Validated quota wall — NOT a task failure. Release to the source
            # phase and do not count a failure while its finite pause is active.
            return _DeadWorker(
                kind, code,
                f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
                "rate_limited",
                payload,
                rate_limited=True,
            )

        # The sentinel proves the result category, but not a finite recovery
        # window. Reuse the reviewed quota classifier and interruption streak
        # instead of entering the indefinitely neutral rate_limited path.
        _category, reason = _kb.classify_infra_exit(
            exit_kind="nonzero_exit", quota_signal=True,
        )
        payload["reason"] = reason
        payload["quota_retry_after_seconds"] = None
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited without a valid retry deadline "
            "(bounded infra interruption)",
            "interrupted",
            payload,
            infra=True,
        )
    # A pending durable timeout-kill intent means THIS dispatcher (or a
    # predecessor that died between signal and reap) sent this SIGTERM/SIGKILL
    # itself via enforce_max_runtime — that always remains a legit, counted
    # failure regardless of which process ends up reaping the worker.
    run_id = _kb._current_run_id(conn, task_id)
    dispatcher_killed = (
        kind == "signaled"
        and _kb.has_pending_timeout_kill_intent(
            conn, task_id=task_id, run_id=run_id, worker_pid=pid,
        )
    )
    if dispatcher_killed:
        _kb.consume_timeout_kill_intent(conn, task_id=task_id, run_id=run_id, worker_pid=pid)
    # Provider quota/429 signature in the worker's final log lines — checked
    # for every non-signaled/non-unknown death too (nonzero_exit is the
    # common case: an AuthError/RateLimitError bubbling up as a plain
    # nonzero exit code instead of the dedicated EX_TEMPFAIL sentinel).
    quota_signal_dict = _kb._detect_quota_exit_signal(task_id, run_id=run_id, board=board)
    # Infra classification: for signaled / nonzero_exit / unknown, consult
    # classify_infra_exit. When infra, the death does NOT count against the
    # failure budget — it is tracked in the interruption streak instead.
    # ``kanban.count_infra_failures=true`` restores pre-classification
    # behaviour wholesale: every death that WOULD be infra-classified is
    # instead routed through the ordinary legit/counted path below.
    if kind in ("signaled", "nonzero_exit", "unknown") and not _kb._count_infra_failures_enabled():
        infra_category, infra_reason = _kb.classify_infra_exit(
            exit_kind=kind,
            signal_number=code if kind == "signaled" else None,
            dispatcher_killed=dispatcher_killed,
            within_startup_window=(
                kind == "unknown"
                and _kb._dispatcher_uptime_seconds() is not None
                and _kb._dispatcher_uptime_seconds() <= _kb._resolve_infra_startup_window_seconds()
            ),
            quota_signal_dict=quota_signal_dict,
        )
        if infra_category == "infra":
            payload = {"pid": pid, "claimer": claimer, "reason": infra_reason}
            if infra_reason == "quota" and quota_signal_dict:
                payload["quota_retry_after_seconds"] = quota_signal_dict.get("retry_after_seconds")
                if quota_signal_dict.get("host_circuit_published"):
                    payload["host_circuit_published"] = True
            error_text = (
                f"pid {pid} {infra_reason} (infra, not counted) "
                f"[exit_kind={kind}"
                + (f", signal={code}" if code is not None else "")
                + "]"
            )
            return _DeadWorker(
                kind, code, error_text, "interrupted", payload,
                infra=True,
            )

    # Legit paths: every code and signal not covered by the allowlist.
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # Parked with the neutral ``review_no_verdict`` outcome: never enters ``crash_details``.
    review_no_verdict: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, protocol_violation, error_text)``: accounted
    # after the txn via ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, bool, str]] = field(default_factory=list)
    # ``(task_id, pid, claimer, error_text)`` for infra-classified deaths: not
    # counted against the failure budget directly, instead bumps the per-task
    # interruption streak (see ``_account_infra_deaths``).
    infra_details: list[tuple[str, int, str, str]] = field(default_factory=list)
    # Task ids classified ``infra`` this tick — surfaced via
    # ``detect_crashed_workers._last_interrupted`` so callers (dispatch result,
    # tests) can distinguish an infra requeue from an actual counted crash.
    # Deliberately NOT included in ``crashed``: the public return value of
    # ``detect_crashed_workers`` must stay crashed-only, exactly like
    # ``rate_limited``/``review_no_verdict`` already do.
    interrupted: list[str] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    preserved_candidates: list[str] = []
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_unit, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _kb._pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead = _classify_dead_worker(
                conn, row["id"], pid, row["claim_lock"], retry_status, board=board,
            )
            dead.event_payload["retry_status"] = retry_status
            # A quota-signature infra death with a usable (parsed + clamped)
            # retry-after AND a resolvable non-``auto`` provider identity is
            # parked in ``scheduled`` instead of re-queued to ``ready`` —
            # every other same-provider task is protected from bouncing off
            # the same 429 wall (see register_provider_backoff / _task_provider).
            target_status = retry_status
            if dead.review_no_verdict:
                target_status = "blocked"
            elif (
                (getattr(dead, "infra", False) or dead.rate_limited)
                and dead.event_payload.get("reason") == "quota"
            ):
                retry_after = dead.event_payload.get("quota_retry_after_seconds")
                # Host-wide protection is account/budget scoped and therefore
                # only activates for an explicit non-secret route mapping. It
                # shares the reviewed quota classifier and deadline parser
                # above; no second classifier or provider-wide inference.
                from hermes_cli import kanban_quota_circuit as _kqc

                budget_group = _kqc.resolve_task_budget_group(conn, row["id"])
                if budget_group and not dead.event_payload.get("host_circuit_published"):
                    circuit = _kqc.register_quota_circuit(
                        budget_group,
                        retry_after=retry_after,
                        board=board or _kb.get_current_board(),
                        task_id=row["id"],
                        reason="quota",
                        max_seconds=_kb._resolve_provider_backoff_max_seconds(),
                    )
                    if circuit is not None:
                        dead.event_payload["budget_group"] = circuit["group"]
                        dead.event_payload["host_resume_at"] = circuit["next_eligible_at"]
                provider = _kb._task_provider(conn, row["id"]) if _kb._provider_backoff_enabled() else None
                if provider:
                    until = _kb.register_provider_backoff(
                        conn, provider=provider, retry_after=retry_after, task_id=row["id"],
                        max_seconds=_kb._resolve_provider_backoff_max_seconds(),
                    )
                    if until is not None:
                        target_status = "scheduled"
                        dead.event_payload["provider"] = provider
                        dead.event_payload["resume_at"] = until
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (target_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if (
                dead.infra
                and dead.kind == "rate_limited"
                and dead.event_payload.get("reason") == "quota"
                and dead.event_payload.get("quota_retry_after_seconds") is None
            ):
                # A rejected deadline supersedes any quota-wall text from the
                # prior run. Leaving that stale text makes blocker_auth stop the
                # bounded interruption sequence after its first increment.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = NULL WHERE id = ?",
                    (row["id"],),
                )
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            elif dead.review_no_verdict:
                # Neutral: no ``last_failure_error`` stamp, never reaches ``_record_task_failure``.
                sweep.review_no_verdict.append(row["id"])
            elif getattr(dead, "infra", False):
                # Infra dead worker: does NOT enter crash_details or the failure
                # budget, and does NOT count toward the ``crashed`` return
                # value either — surfaced separately via ``interrupted`` /
                # ``_last_interrupted`` so callers can see which tasks were
                # infra-classified this tick without conflating them with
                # actual counted crashes.
                sweep.interrupted.append(row["id"])
                sweep.infra_details.append(
                    (row["id"], pid, row["claim_lock"], dead.error_text)
                )
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append(
                    (row["id"], pid, row["claim_lock"], dead.protocol_violation, dead.error_text)
                )
            preserved_candidates.append(row["id"])
    # Preservation runs AFTER the sweep transaction commits (it does slow git
    # work and records its own event, so it must not sit inside this write
    # txn), but still BEFORE any spawn in this tick — so a retry can never be
    # started onto a worktree whose previous run's output was not yet saved.
    # No expected_run_id: ``_end_run`` already cleared ``current_run_id``, and
    # the pid these tasks belonged to was verified dead above.
    for task_id in preserved_candidates:
        _kb._preserve_task_work(conn, task_id)
    return sweep


def _account_infra_deaths(
    conn: sqlite3.Connection, infra_details: list[tuple[str, int, str, str]],
) -> list[str]:
    """Bump the per-task interruption streak for each infra death and possibly
    promote to a legit counted crash when ``max_infra_interruptions`` is exceeded.

    Infra deaths (signaled-by-allowlist SIGTERM/SIGKILL, or unknown-within-
    startup-window) are NOT counted in ``consecutive_failures``. Instead each
    bump is recorded against a separate per-task streak. When the streak exceeds
    the configured cap the task is fed to ``_record_task_failure`` as a normal
    crashed failure so the bounded retry / circuit breaker still eventually
    applies — the infra window only suppresses the FIRST N infrastructure
    deaths, not forever.

    Streak is reset ONLY on a non-interruption terminal outcome or an explicit
    operator reset; a redispatch or protocol-violation retry never clears it.

    Returns the task ids promoted to a counted crash this call (streak
    exceeded the cap) — the caller removes these from the ``interrupted``
    side-channel list since they are no longer a neutral outcome.
    """
    promoted: list[str] = []
    if not infra_details:
        return promoted
    max_allowed = _kb._resolve_max_infra_interruptions()
    for tid, pid, claimer, error_text in infra_details:
        streak = _kb.increment_interruption_streak(conn, task_id=tid)
        if streak > max_allowed:
            # Promote to a legit counted crash: fed to the breaker exactly like
            # a today crash. ``force_trip`` because the decision was made against
            # the infra cap, not the normal failure counter. The streak is
            # PRESERVED (not reset) here — only a genuine non-interruption
            # terminal outcome or an explicit operator reset clears it, so a
            # task that keeps dying to interruptions cannot loop through the
            # cap forever by getting a few real successes in between.
            _record_task_failure(
                conn, tid,
                error=(
                    f"{error_text} [infra interruption streak {streak} exceeded "
                    f"kanban.max_infra_interruptions={max_allowed}; routed through "
                    "normal counted-failure accounting]"
                ),
                outcome="crashed",
                failure_limit=max_allowed,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid, "claimer": claimer, "infra_streak": streak,
                    "infra_streak_cap": max_allowed,
                },
            )
            promoted.append(tid)
    return promoted


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) trip
    immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, _, err_text in crash_details:
        fp = _error_fingerprint(err_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, protocol_violation, error_text in crash_details:
        if protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer},
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board)
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = _account_crashes(conn, sweep.crash_details) if sweep.crash_details else []
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    # Reviewer no-verdict parks: not a failure, not a crash, and (unlike rate-limited) not
    # auto-recoverable — sticky in ``blocked`` until an explicit ``kanban_unblock``.
    detect_crashed_workers._last_review_no_verdict = sweep.review_no_verdict  # type: ignore[attr-defined]
    # Infra dead workers: bump the per-task interruption streak. When the
    # configured cap (``max_infra_interruptions``) is exceeded the task is
    # promoted to a legit counted crash (fed to _record_task_failure) so the
    # bounded retry / breaker still applies — the infra window only suppresses
    # the FIRST N infrastructure deaths, not forever.
    promoted = _account_infra_deaths(conn, sweep.infra_details) if sweep.infra_details else []
    detect_crashed_workers._last_interrupted = (  # type: ignore[attr-defined]
        [tid for tid in sweep.interrupted if tid not in promoted]
    )
    if promoted:
        # force_trip=True always trips inside _record_task_failure, so every
        # promoted id auto-blocked; fold into the public auto_blocked side-
        # channel so DispatchResult.auto_blocked reflects it too.
        auto_blocked.extend(promoted)
        detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
        # A streak-exceeded infra death is, from the caller's perspective, now
        # an ordinary accounted crash — fold it into the public return value
        # too so callers that only look at the return list (not the
        # ``_last_interrupted``/``_last_auto_blocked`` side channels) still
        # see it.
        sweep.crashed.extend(promoted)

    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + 1

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        effective_limit, limit_source = effective_failure_limit(task_override, failure_limit)

        if not (force_trip or failures >= effective_limit):
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error,
                    metadata={"failures": failures, "retry_status": retry_status},
                )
                _kb._append_event(
                    conn, task_id, outcome,
                    {"error": error, "failures": failures, "retry_status": retry_status},
                    run_id=run_id,
                )
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did. ``scheduled`` is included
        # alongside ``ready``/``review`` because a quota-parked task (provider
        # backoff) can reach this trip branch via the infra-interruption cap
        # (_account_infra_deaths) while still sitting in ``scheduled``.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_unit = NULL, "
               if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review', 'scheduled')",
            (failures, error, task_id),
        )
        payload = {
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
            # Durable marker consulted by ``_gave_up_was_force_tripped``: a force-tripped breaker
            # bypassed the counter-vs-threshold comparison, so ``consecutive_failures`` at trip time
            # can sit under whatever limit ``recompute_ready`` re-derives; without this it would
            # auto-promote a ``gave_up`` the breaker just tripped (the gave_up -> promoted ->
            # claimed loop). force_trip=True is sticky until ``kanban_unblock``.
            "force_trip": bool(force_trip),
        }
        run_id = None
        if end_run:
            # Only the spawn path has an open run to close.
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures,
                    "trigger_outcome": outcome,
                    "effective_limit": effective_limit,
                    "limit_source": limit_source,
                    "retry_status": retry_status,
                },
            )
        if event_payload_extra:
            payload.update(event_payload_extra)
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        return True


def _set_worker_pid(
    conn: sqlite3.Connection, task_id: str, pid: int, *, worker_unit: Optional[str] = None,
    task: Optional[Task] = None,
) -> None:
    """Record the spawned child's pid and immutable launch analytics.

    ``worker_unit`` is only non-None when ``kanban.worker_launcher`` produced a
    ``--unit=`` scope. Launch analytics were already persisted before spawn;
    this event reads them back from the run so its payload is self-describing.
    """
    with _kb.write_txn(conn):
        if worker_unit:
            conn.execute(
                "UPDATE tasks SET worker_pid = ?, worker_unit = ? WHERE id = ?",
                (int(pid), worker_unit, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (int(pid), task_id))
        run_id = (
            task.current_run_id
            if task is not None and task.current_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        launch_row = (
            conn.execute(
                "SELECT session_id, model, provider, reasoning_effort, model_source "
                "FROM task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run_id is not None
            else None
        )
        analytics = {
            key: launch_row[key] if launch_row is not None else None
            for key in ("model", "provider", "reasoning_effort", "model_source")
        }
        session_id = launch_row["session_id"] if launch_row is not None else None
        if run_id is not None:
            conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (int(pid), run_id))
        payload: dict[str, Any] = {
            "pid": int(pid),
            "session_id": session_id,
            **analytics,
        }
        if worker_unit:
            payload["worker_unit"] = worker_unit
        _kb._append_event(conn, task_id, "spawned", payload, run_id=run_id)


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def check_respawn_guard(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    lane: str = "ready",
    board: Optional[str] = None,
    consume_host_probe: bool = False,
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"blocker_auth"``
    (quota/auth pattern; the breaker still trips eventually), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (a GitHub PR URL, from a comment AUTHORED BY this task's own assignee, whose
    owner/repo matches this task's own repo when that repo is resolvable —
    re-spawning risks a duplicate PR). The review lane skips the last two: they
    are the *inputs* to a review handoff. Stale / dead claim locks are NOT a
    guard reason — the reclaim passes own those.
    """
    row = conn.execute(
        "SELECT last_failure_error, assignee FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 0. Host-wide account/budget circuit. It is inert unless the operator
    # explicitly mapped this provider/profile route to an opaque group.
    from hermes_cli import kanban_quota_circuit as _kqc

    host_guard = _kqc.task_quota_guard(
        conn,
        task_id,
        board=board,
        consume_probe=consume_host_probe,
    )
    if host_guard is not None:
        return host_guard

    # 0a. Per-board provider-wide pause is checked next in both lanes. Unlike the
    #    per-task rate-limit cooldown below, this protects every task
    #    explicitly pinned to the exhausted provider while allowing other
    #    providers (and ``provider: auto`` tasks, which can resolve
    #    elsewhere) to continue.
    if _kb._provider_backoff_enabled():
        provider = _kb._task_provider(conn, task_id)
        if provider and _kb.provider_backoff_until(conn, provider=provider) is not None:
            return "provider_backoff"

    # 1. Rate-limit cooldown — see docstring for why this precedes blocker_auth.
    #    LATEST run only: a newer crash/completion supersedes the rate-limit run.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, skipping blocker_auth so
            # the stamped rate-limit text doesn't re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — return early so blocker_auth doesn't catch the
        # stamped rate-limit text; this path intentionally retries forever
        # (spaced by the cooldown) until quota returns or a real run supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
    #    Two independent scopes must BOTH hold, because either alone still lets
    #    an unrelated PR guard the card:
    #      - Author: only a comment from THIS task's own assignee (a worker who
    #        actually ran on this card) counts. A human/orchestrator/reviewer
    #        note quoting a PR for context (prior art, "see also") never guards
    #        — it isn't proof this card opened anything.
    #      - Repo: when the task's own repo is resolvable (project link or a
    #        worktree workspace's origin remote), the cited PR's owner/repo
    #        must match it. A worker's own comment linking an unrelated repo's
    #        PR (e.g. quoting an upstream issue while researching prior art)
    #        still must not guard. When the repo can't be determined (e.g. a
    #        scratch board-only task with no code) this scope is skipped —
    #        author-scoping alone is enough signal there.
    assignee = row["assignee"]
    own_repo_slug = _task_own_repo_slug(conn, task_id)
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff),
    ).fetchall():
        if not c["body"] or not assignee or c["author"] != assignee:
            continue
        for match in _RESPAWN_GUARD_PR_URL_RE.finditer(c["body"]):
            if own_repo_slug is None:
                return "active_pr"
            cited_slug = f"{match.group('owner')}/{match.group('repo')}".lower()
            if cited_slug == own_repo_slug:
                return "active_pr"

    return None


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``hermes_cli.profiles.profile_exists``, or ``None`` when it cannot be
    imported (local import avoids a cycle; callers fall back to trusting the
    assignee)."""
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        return None
    return profile_exists


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall()
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when ``profile_exists`` is unimportable.
    """
    return _has_spawnable(conn, "ready")


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


@dataclass(frozen=True)
class DispatchCaps:
    """Resolved ``kanban.*`` concurrency settings for one ``dispatch_once`` call.

    ``max_in_progress`` is already routed through :func:`resolve_max_in_progress`,
    so it carries the memory-derived default when config leaves it unset.
    """

    max_in_progress: Optional[int]
    max_in_progress_per_profile: Optional[int]
    max_spawn: Optional[int]
    default_assignee: Optional[str]
    # kanban.default_reviewer rides on the same shared resolution as the caps:
    # every dispatch_once entry point must route review-lane cards identically,
    # and an entry point that resolves caps but not the reviewer would silently
    # leave review cards self-assigned to their implementer.
    default_reviewer: Optional[str] = None
    dispatch_start_budget: Optional[int] = None
    dispatch_start_window_seconds: int = 600
    review_rework_escalation_profile: Optional[str] = None
    # Hard stop on the review<->changes_requested loop (kanban.max_review_rounds). Always a
    # concrete int (0 = unlimited) — unlike the Optional caps above, "not configured" and
    # "explicitly disabled" both resolve to a number the dispatcher can compare directly.
    max_review_rounds: int = DEFAULT_MAX_REVIEW_ROUNDS


def resolve_dispatch_caps(kanban_cfg: Optional[dict] = None) -> DispatchCaps:
    """Resolve the concurrency caps every ``dispatch_once`` entry point must honour.

    The caps bound the HOST, so they cannot be a property of one entry point:
    the gateway's periodic tick, ``hermes kanban dispatch`` and the dashboard's
    ``POST /dispatch`` nudge all spawn real workers against the same CPU and
    memory. An entry point that skips this resolution does not merely dispatch
    "differently" — it dispatches *uncapped*, because ``dispatch_once`` treats
    ``None`` as unlimited. Keeping the parsing here means adding a fourth caller
    cannot reintroduce that gap by omission.

    Reads config itself when *kanban_cfg* is None. Fails open to all-``None``
    only on a config-read error, which is the pre-existing behaviour of every
    caller — a broken config must not wedge dispatch entirely.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config()
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception:
            _kb._log.warning(
                "kanban dispatch: config unreadable; proceeding without configured caps"
            )
            kanban_cfg = {}
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}

    return DispatchCaps(
        max_in_progress=resolve_max_in_progress(
            _positive_int_or_none(kanban_cfg.get("max_in_progress"))
        ),
        max_in_progress_per_profile=_positive_int_or_none(
            kanban_cfg.get("max_in_progress_per_profile")
        ),
        max_spawn=_positive_int_or_none(kanban_cfg.get("max_spawn")),
        default_assignee=(kanban_cfg.get("default_assignee") or "").strip() or None,
        default_reviewer=(kanban_cfg.get("default_reviewer") or "").strip() or None,
        dispatch_start_budget=_positive_int_or_none(
            kanban_cfg.get("dispatch_start_budget")
        ),
        dispatch_start_window_seconds=_positive_int(
            kanban_cfg.get("dispatch_start_window_seconds"), 600,
        ),
        review_rework_escalation_profile=(
            kanban_cfg.get("review_rework_escalation_profile") or ""
        ).strip() or None,
        max_review_rounds=_nonnegative_int(
            kanban_cfg.get("max_review_rounds"), DEFAULT_MAX_REVIEW_ROUNDS,
        ),
    )


def clamp_requested_max_spawn(
    requested: Optional[int], caps: "DispatchCaps"
) -> Optional[int]:
    """Narrow a caller-supplied request to the resolved HOST cap; never widen.

    Only for values that arrive from outside the operator's config — the
    dashboard nudge reads ``?max=`` straight off a query string, so an
    unclamped value lets a hand-crafted ``?max=99`` ask for more than the host
    allows. Clamps against ``max_in_progress`` alone: ``max_spawn`` is a
    separate per-board axis that ``dispatch_once`` enforces on its own, and
    folding it in here would silently tighten a cap the operator set
    deliberately.

    ``None`` on either side means that side imposes no bound.
    """
    bounds = [b for b in (requested, caps.max_in_progress) if b is not None]
    return min(bounds) if bounds else None


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) yields 0. Fails open per board.
    """
    # A path pin identifies one physical DB even when callers enumerate it by
    # several board slugs. Do not turn those slugs into explicit cross-board
    # requests here: this internal sweep has no such intent, and doing so would
    # double-count workers (and re-enable the multiplied-cap bug).
    pinned = bool(os.environ.get("HERMES_KANBAN_DB", "").strip())
    try:
        current_path = str(_kb.kanban_db_path(board=None if pinned else board).expanduser().resolve())
    except Exception:
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=None if pinned else slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=None if pinned else slug)
            try:
                total += count_running_tasks(other)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return total


def count_running_tasks_by_assignee_other_boards(board: Optional[str] = None) -> dict[str, int]:
    """Return running-worker counts per assignee on every board except ``board``.

    Per-profile concurrency is host-wide just like ``max_in_progress``: a
    profile may be assigned work from any board, but its model/API quota is one
    shared resource. A path pin represents one DB, so every enumerated slug
    remains pinned for this internal sweep just as in
    :func:`count_running_tasks_other_boards`.
    """
    pinned = bool(os.environ.get("HERMES_KANBAN_DB", "").strip())
    try:
        current_path = str(_kb.kanban_db_path(board=None if pinned else board).expanduser().resolve())
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=None if pinned else slug).expanduser()
            if str(path.resolve()) == current_path or not path.exists():
                continue
            other = _kbc.connect(board=None if pinned else slug)
            try:
                rows = other.execute(
                    "SELECT assignee, COUNT(*) AS n FROM tasks "
                    "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
                )
                for row in rows:
                    assignee = row["assignee"]
                    counts[assignee] = counts.get(assignee, 0) + int(row["n"])
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return counts


def count_running_tasks_by_assignee(conn: sqlite3.Connection, board: Optional[str] = None) -> dict[str, int]:
    """Host-wide running-worker counts per assignee: this board's rows plus every
    other board's (:func:`count_running_tasks_by_assignee_other_boards`).

    Single source of truth for "how many workers does profile X have in flight
    right now" — both the dispatcher's per-profile cap enforcement and
    diagnostics' concurrency-aware ``stranded_in_ready`` rule read this so they
    can never drift into two counters that disagree.
    """
    counts = count_running_tasks_by_assignee_other_boards(board)
    for prow in conn.execute(
        "SELECT assignee, COUNT(*) AS n FROM tasks "
        "WHERE status = 'running' AND assignee IS NOT NULL "
        "GROUP BY assignee"
    ):
        assignee = prow["assignee"]
        counts[assignee] = counts.get(assignee, 0) + int(prow["n"])
    return counts


def total_running_tasks(conn: sqlite3.Connection, board: Optional[str] = None) -> int:
    """Host-wide running-worker count: this board's rows (:func:`count_running_tasks`)
    plus every other board's (:func:`count_running_tasks_other_boards`).

    Shared so the dispatcher's ``max_in_progress`` enforcement and diagnostics'
    concurrency-aware rules agree on the same number.
    """
    return count_running_tasks(conn) + count_running_tasks_other_boards(board)


def concurrency_snapshot(conn: sqlite3.Connection, board: Optional[str] = None,
                          *, kanban_cfg: Optional[dict] = None) -> dict:
    """Host concurrency snapshot for concurrency-aware diagnostics.

    Resolves the same caps (:func:`resolve_dispatch_caps`) and running-task
    counts (:func:`total_running_tasks` / :func:`count_running_tasks_by_assignee`)
    the dispatcher itself uses to enforce ``kanban.max_in_progress`` /
    ``kanban.max_in_progress_per_profile``. Callers (dashboard/CLI diagnostics)
    pass the result into ``kanban_diagnostics.compute_task_diagnostics(...,
    concurrency=...)`` so ``stranded_in_ready`` can tell "queued behind a full
    pipe" from "actually stuck" without reimplementing a second counter that
    can drift from the enforcer.
    """
    caps = resolve_dispatch_caps(kanban_cfg)
    return {
        "max_in_progress": caps.max_in_progress,
        "max_in_progress_per_profile": caps.max_in_progress_per_profile,
        "total_running": total_running_tasks(conn, board),
        "running_by_assignee": count_running_tasks_by_assignee(conn, board),
    }


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


OPERATOR_PAUSE_REASON = "operator_paused"
"""Pause reason for a deliberate operator maintenance drain.

Distinct from the self-expiring ``start_budget_exceeded`` cooldown and from the
fault circuits (``restart_safe_scope_unavailable``, ``pause_persistence_failed``)
so "why is this paused" stays a stable, machine-readable record.
"""


def _dispatch_pause_path(board: Optional[str]) -> Path:
    """Sticky circuit state beside the resolved board database.

    Deriving this from :func:`kanban_db_path` preserves ``HERMES_KANBAN_DB``
    sandbox/path-pin isolation. A test or worker pinned to another database must
    never trip or resume the live board's circuit.
    """
    return _kb.kanban_db_path(board).with_suffix(".dispatch-pause.json")


def _valid_start_budget_pause_state(state: Mapping[str, Any]) -> bool:
    """Recognize current and legacy cooldown records without trusting bare reasons."""
    recent_starts = state.get("recent_starts")
    budget = state.get("budget")
    window_seconds = state.get("window_seconds")
    values = (recent_starts, budget, window_seconds)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return False
    assert isinstance(recent_starts, int)
    assert isinstance(budget, int)
    assert isinstance(window_seconds, int)
    return recent_starts >= 0 and budget > 0 and window_seconds > 0


def read_dispatch_pause(board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the board's current dispatch stop state, if any.

    The SQLite fallback exists only for a sticky systemic fault and therefore
    takes precedence over the JSON sentinel, which may contain a self-expiring
    start-budget cooldown. Unreadable state fails closed; silently treating a
    damaged safety record as absent widens dispatch.
    """
    from hermes_cli.kanban_db_dispatch_circuit import read_pause

    path = _dispatch_pause_path(board)
    try:
        fallback = read_pause(_kb.kanban_db_path(board))
        if fallback is not None:
            return fallback
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        raw = json.loads(text)
        if not isinstance(raw, dict) or not raw.get("reason"):
            raise ValueError("pause state must be an object with a reason")
        if raw["reason"] == "start_budget_exceeded" and not _valid_start_budget_pause_state(raw):
            raise ValueError("start-budget cooldown state is missing required fields")
        return raw
    except Exception as exc:
        return {
            "reason": "pause_state_unreadable",
            "detail": str(exc),
            "path": str(path),
            "recovery": "repair dispatch-pause storage, then run `hermes kanban dispatch --resume-circuit`",
        }


def _write_dispatch_pause(
    board: Optional[str], reason: str, *, replace: bool = False, **details: Any,
) -> dict[str, Any]:
    """Atomically persist a board pause or rate-limit cooldown state."""
    current = read_dispatch_pause(board)
    if current is not None:
        if not replace:
            return current
        if current.get("reason") == reason and all(
            current.get(key) == value for key, value in details.items()
        ):
            return current
    state: dict[str, Any] = {
        "reason": reason,
        "paused_at": int(time.time()),
        **details,
    }
    path = _dispatch_pause_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    _kb._log.warning(
        "kanban dispatch for board %s: %s",
        board or _kb.DEFAULT_BOARD,
        dispatch_pause_message(state, board=board),
    )
    return state


def _clear_expired_start_budget_pause(board: Optional[str]) -> None:
    """Remove the normal cooldown only while the dispatch tick lock is held."""
    with contextlib.suppress(FileNotFoundError):
        _dispatch_pause_path(board).unlink()


def _recent_dispatch_start_window(
    conn: sqlite3.Connection, *, window_seconds: int, budget: int = 1,
    now: Optional[int] = None,
) -> tuple[int, Optional[int]]:
    """Return starts in the inclusive window and the exact next eligible time.

    When a live reload changes the budget, more than one in-window start may
    need to age out before another start is legal. The required expiry is the
    ``count - budget``-indexed start, not always the oldest one: after it
    leaves the inclusive window, exactly ``budget - 1`` starts remain.
    """
    current = int(now if now is not None else time.time())
    cutoff = current - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM task_events "
        "WHERE kind = 'spawned' AND created_at >= ?",
        (cutoff,),
    ).fetchone()
    starts = int(row["count"])
    if starts < budget:
        return starts, None
    expiry_row = conn.execute(
        "SELECT created_at FROM task_events WHERE kind = 'spawned' AND created_at >= ? "
        "ORDER BY created_at, id LIMIT 1 OFFSET ?",
        (cutoff, starts - budget),
    ).fetchone()
    # The query includes the cutoff boundary, so capacity returns one second
    # after the final required start is no longer in the measured interval.
    return starts, int(expiry_row["created_at"]) + window_seconds + 1


def dispatch_pause_message(state: Mapping[str, Any], *, board: Optional[str] = None) -> str:
    """One status message for CLI, gateway-backed dashboard, and API callers."""
    if state.get("reason") == "start_budget_exceeded":
        next_eligible = state.get("next_eligible_at")
        if isinstance(next_eligible, int):
            when = datetime.fromtimestamp(next_eligible, tz=timezone.utc).isoformat()
            return f"rate limited until {when}; dispatch resumes automatically"
        return "rate limited; dispatch resumes automatically when capacity is available"
    command = "hermes kanban "
    if board:
        command += f"--board {board} "
    command += "dispatch --resume-circuit"
    if state.get("reason") == OPERATOR_PAUSE_REASON:
        # A deliberate maintenance drain is not a fault: rendering it with the
        # generic "manual intervention required" phrasing below would report a
        # healthy, intentionally-stopped board as broken.
        context = []
        if state.get("paused_by"):
            context.append(f"by={state['paused_by']}")
        if isinstance(state.get("paused_at"), int):
            context.append(
                f"at={datetime.fromtimestamp(state['paused_at'], tz=timezone.utc).isoformat()}"
            )
        if state.get("note"):
            context.append(f"note={state['note']}")
        suffix = f" ({'; '.join(context)})" if context else ""
        return (
            f"paused for maintenance{suffix}; already-running workers are unaffected; "
            f"resume with: {command}"
        )
    details = [f"reason={state.get('reason', 'unknown pause')}"]
    if state.get("fault_code"):
        details.append(f"fault_code={state['fault_code']}")
    paused_at = state.get("tripped_at") or state.get("paused_at")
    if paused_at:
        details.append(f"time={paused_at}")
    if state.get("recovery"):
        details.append(f"recovery={state['recovery']}")
    return (
        f"manual intervention required ({'; '.join(details)}); "
        f"resume explicitly with: {command}"
    )


def pause_dispatch(board: Optional[str] = None, *, note: Optional[str] = None) -> dict[str, Any]:
    """Deliberately stop this board claiming/spawning new workers.

    The operator counterpart to :func:`resume_dispatch`, for draining a board
    before a gateway/service restart: workers share the gateway's
    ``KillMode=mixed`` cgroup, so restarting while any are running SIGKILLs
    them and discards uncommitted worktree progress.

    This only fences NEW dispatch — ``_dispatch_once_locked`` returns early on
    a live pause and there is deliberately no kill/reclaim behaviour here, so
    already-running workers keep running and can still complete or block
    normally while the board drains.

    Idempotent, and never overrides an existing pause: re-pausing returns the
    current state untouched so the first (possibly fault-written) "why is this
    paused" record and its recovery guidance survive.
    """
    _kb._assert_not_delegated_child_mutation()
    db_path = _kb.kanban_db_path(board=board)
    # Same discipline as resume_dispatch: a tick in flight may be about to
    # write a fault pause of its own, and either side landing inside that
    # window would silently clobber the other. Refusing keeps the operator
    # action deliberate — retry once the tick finishes.
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return {"paused": False, "state": None, "reason": "dispatch_in_progress"}
        details: dict[str, Any] = {"paused_by": _kb._hook_profile_name()}
        if note:
            details["note"] = note
        state = _write_dispatch_pause(board, OPERATOR_PAUSE_REASON, **details)
    return {"paused": True, "state": state}


def resume_dispatch(board: Optional[str] = None) -> dict[str, Any]:
    """Explicitly clear a board safety pause or current rate-limit status."""
    from hermes_cli.kanban_db_dispatch_circuit import clear_pause
    _kb._assert_not_delegated_child_mutation()
    db_path = _kb.kanban_db_path(board=board)
    # The pause check and its removal must share the dispatch tick's board lock.
    # Otherwise a tick can pass its check, write a fresh pause, and then have
    # this operator action unlink that newer safety state. Refusing a contended
    # resume makes recovery deliberate: repair, then re-run the explicit probe.
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return {
                "was_paused": read_dispatch_pause(board) is not None,
                "resumed": False,
                "reason": "dispatch_in_progress",
            }
        path = _dispatch_pause_path(board)
        previous = read_dispatch_pause(board)
        # The operator changed their mind about the maintenance window, so any
        # action queued to fire when this board drained is no longer wanted.
        # Cancelled under the same board lock that clears the pause: resuming
        # and leaving a reboot armed would be the worst possible split outcome.
        # ``_cancel_locked`` is the lock-HELD variant — the public
        # ``cancel_post_drain_action`` would try to re-acquire the tick lock we
        # are already holding, see the non-blocking guard decline against our
        # own hold, and silently leave the action armed.
        try:
            from hermes_cli.kanban_db_dispatch_postdrain import _cancel_locked
            _cancel_locked(board, reason="dispatch resumed")
        except Exception:
            _kb._log.warning(
                "kanban dispatch for board %s: could not cancel the queued post-drain action",
                board or _kb.DEFAULT_BOARD, exc_info=True,
            )
        # Clear SQLite first. A JSON-only circuit must remain authoritative if
        # fallback cleanup fails; unlinking it first would silently re-arm the
        # next tick even though this explicit recovery returned an error.
        # Conversely, if the later unlink fails, the JSON sentinel still
        # fences dispatch. Both stores are cleared under the board lock.
        clear_pause(db_path)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return {"was_paused": previous is not None, "previous": previous, "resumed": True}


def _is_shared_launcher_prerequisite_fault(exc: BaseException) -> bool:
    """Whether *exc* is the one host-scoped launch fault the board can share.

    The type originates exactly where the restart-safe user-scope availability
    probe fails.  Do not broaden this to message matching: credentials,
    workspace errors, and a disappearing launcher are task-local failures and
    must retain the normal per-card retry semantics.
    """
    from tools.process_registry import RestartSafeScopeUnavailable

    return isinstance(exc, RestartSafeScopeUnavailable)


def _defer_for_shared_launcher_prerequisite(
    conn: sqlite3.Connection,
    task: Task,
    exc: BaseException,
    result: DispatchResult,
    *,
    board: Optional[str],
) -> None:
    """Release one claimed task and pause its board without charging the task.

    The dispatcher lock makes the pause visible before another tick can claim a
    sibling.  The triggering run is retained as a non-failure ``spawn_deferred``
    receipt, while every task failure field remains untouched.
    """
    from tools.process_registry import RestartSafeScopeUnavailable

    fault_code = RestartSafeScopeUnavailable.fault_code
    error = str(exc)[:500]
    tripped_at = int(time.time())
    try:
        state = _write_dispatch_pause(
            board,
            "restart_safe_scope_unavailable",
            fault_code=fault_code,
            trigger_task_id=task.id,
            error=error,
            tripped_at=tripped_at,
            recovery="repair the user scope prerequisite, then run `hermes kanban dispatch --resume-circuit`",
        )
        event_kind = "dispatch_circuit_tripped"
    except Exception as pause_error:
        # A failure to persist the normal sentinel must never strand the
        # already-claimed trigger or charge its task-local budget. Persist the
        # fallback in SQLite with the run receipt so later ticks/restarts see
        # it too, independently of the triggering task's lifecycle.
        state = {
            "reason": "pause_persistence_failed",
            "fault_code": fault_code,
            "trigger_task_id": task.id,
            "error": error,
            "tripped_at": tripped_at,
            "pause_error": str(pause_error)[:500],
            "recovery": "repair dispatch-pause storage and the user scope prerequisite, then run `hermes kanban dispatch --resume-circuit`",
        }
        event_kind = "dispatch_circuit_persistence_failed"
    # The durable pause is authoritative for later ticks, but this tick must
    # stop immediately even if a concurrent reconciliation wins the task-row
    # compare-and-swap below.
    result.dispatch_paused = state
    with _kb.write_txn(conn):
        if event_kind == "dispatch_circuit_persistence_failed":
            from hermes_cli.kanban_db_dispatch_circuit import persist_pause

            persist_pause(conn, state)
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'running'",
            (task.id,),
        ).fetchone()
        if row is not None:
            retry_status = _kb._retry_status_for_run(conn, task.id, row["current_run_id"])
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_unit = NULL WHERE id = ? AND status = 'running'",
                (retry_status, task.id),
            )
            if cur.rowcount == 1:
                run_id = _kb._end_run(
                    conn,
                    task.id,
                    outcome="spawn_deferred",
                    status="spawn_deferred",
                    error=error,
                    metadata={"fault_code": fault_code, "board_circuit": state},
                )
                _kb._append_event(
                    conn,
                    task.id,
                    event_kind,
                    {"fault_code": fault_code, "board": board or _kb.DEFAULT_BOARD,
                     "tripped_at": tripped_at, "error": error, "recovery": state["recovery"],
                     "pause_error": state.get("pause_error")},
                    run_id=run_id,
                )


def _recent_dispatch_starts(
    conn: sqlite3.Connection, *, window_seconds: int, now: Optional[int] = None,
) -> int:
    return _recent_dispatch_start_window(
        conn, window_seconds=window_seconds, now=now,
    )[0]


def _terminal_card_replay_ids(conn: sqlite3.Connection) -> list[str]:
    """Dispatchable cards with terminal completion but no sanctioned reopen."""
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('ready', 'review') "
        "ORDER BY created_at, id"
    ).fetchall()
    return [
        str(row["id"])
        for row in rows
        if _kb._terminal_completion_without_reopen(conn, str(row["id"])) is not None
    ]


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    default_reviewer: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    dispatch_start_budget: Optional[int] = None,
    dispatch_start_window_seconds: int = 600,
    review_rework_escalation_profile: Optional[str] = None,
    max_review_rounds: Optional[int] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.
    """
    def _locked_tick() -> DispatchResult:
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            default_reviewer=default_reviewer,
            max_in_progress_per_profile=max_in_progress_per_profile,
            dispatch_start_budget=dispatch_start_budget,
            dispatch_start_window_seconds=dispatch_start_window_seconds,
            review_rework_escalation_profile=review_rework_escalation_profile,
            max_review_rounds=max_review_rounds,
            reconcile_orphans=reconcile_orphans,
        )

    needs_host_cap_lock = (
        max_in_progress is not None or max_in_progress_per_profile is not None
    )
    try:
        db_path = _kb.kanban_db_path(board=board)
    except Exception:
        # Preserve dispatch availability when the board path cannot be resolved.
        db_path = None

    host_lock = (
        _kbc._host_dispatch_cap_lock()
        if needs_host_cap_lock else contextlib.nullcontext(True)
    )
    with host_lock as host_held:
        if not host_held:
            result = DispatchResult(skipped_locked=True)
        elif db_path is None:
            result = _locked_tick()
        else:
            with _kbc._dispatch_tick_lock(db_path) as board_held:
                if not board_held:
                    result = DispatchResult(skipped_locked=True)
                else:
                    result = _locked_tick()
                    # Still under the board dispatch lock: periodic PASSIVE WAL checkpoint.
                    _kbc._maybe_checkpoint_wal(conn, db_path)
    # Locks released. Fire the tick observer strictly OUTSIDE the critical
    # section: a slow subscriber must never stall a sibling dispatcher's tick.
    _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    # Post-drain action queue: this is the server-side trigger, so a queued
    # restart/reboot fires from the dispatcher's own tick with no browser open.
    # It must run with the board tick lock RELEASED — the evaluation re-takes
    # that same lock to claim ``waiting -> firing`` atomically, and the
    # non-blocking guard would simply decline against our own hold. A dry run
    # reports what a tick would do and must never fire a real side effect.
    if not dry_run:
        try:
            from hermes_cli.kanban_db_dispatch_postdrain import evaluate_post_drain_action
            evaluate_post_drain_action(board)
        except Exception:
            # A queue fault must never take dispatch down with it: the record
            # stays where it is and the next tick re-evaluates.
            _kb._log.warning(
                "kanban dispatch for board %s: post-drain action evaluation failed",
                board or _kb.DEFAULT_BOARD, exc_info=True,
            )
    return result


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _serialize_coedit(
    conn: sqlite3.Connection,
    task_id: str,
    tenant: Optional[str],
    paths: list[str],
    coedit_index,
    result: "DispatchResult",
    *,
    dry_run: bool,
) -> bool:
    """Park ``task_id`` behind whichever running card already owns one of its
    declared edit targets. Returns True when the card was deferred.

    Serialization, not blocking: the card gets a real ``parents=[holder]`` edge
    so it waits and then starts from a tree that already contains the holder's
    work. Blocking for a human would be a routing bug — this fleet runs
    unattended.
    """
    if not paths:
        return False
    collision = coedit_index.holder_for(tenant, paths)
    if collision is None:
        return False
    holder_id, path = collision
    if holder_id == task_id:
        return False
    result.serialized_coedit.append((task_id, holder_id, path))
    if dry_run:
        return True
    try:
        # link_tasks demotes a ready child to todo and refuses a cycle, so an
        # already-linked or circular pair degrades to "leave it alone" rather
        # than corrupting the graph.
        _kb.link_tasks(conn, holder_id, task_id)
    except ValueError:
        # Cycle or a vanished row: the edge is unsafe, so let the card dispatch
        # normally rather than stranding it.
        result.serialized_coedit.pop()
        return False
    with _kb.write_txn(conn):
        _kb._append_event(
            conn, task_id, "serialized_coedit",
            {"holder": holder_id, "path": path},
        )
    return True


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: "DispatchResult",
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
    coedit_index=None,
    coedit_paths: Optional[dict] = None,
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return False
    guard_reason = check_respawn_guard(
        conn,
        task_id,
        lane=lane,
        board=board,
        consume_host_probe=not dry_run,
    )
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck.
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "respawn_guarded", {"reason": guard_reason})
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent
        # ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    # Co-edit serialization. Only the ready lane: a review card reads the branch
    # its implementer already produced, so it is not a concurrent writer.
    own_paths = (coedit_paths or {}).get(task_id, []) if coedit_paths else []
    if lane == "ready" and coedit_index is not None and own_paths:
        if _serialize_coedit(
            conn, task_id, row["tenant"] if "tenant" in row.keys() else None,
            own_paths, coedit_index, result, dry_run=dry_run,
        ):
            return False

    def _claim_coedit_paths(claimed_id: str, tenant: Optional[str]) -> None:
        """This card now owns its declared paths for the rest of the tick, so a
        later ready row in the SAME tick serializes behind it too."""
        if coedit_index is not None and own_paths:
            coedit_index.claim(claimed_id, tenant, own_paths)

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        _claim_coedit_paths(task_id, row["tenant"] if "tenant" in row.keys() else None)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    if claimed is None:
        return False
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    try:
        # Resolve and persist before invoking either the built-in or a
        # compatible custom spawn function. This closes the race where a very
        # fast worker finalizes its run before the parent records its PID.
        _prepare_worker_launch(claimed)
        _stamp_worker_run_launch(conn, claimed)
        pid = _call_spawn_fn(spawn_fn if spawn_fn is not None else _default_spawn, claimed, str(workspace), board)
        if pid:
            _set_worker_pid(
                conn, claimed.id, int(pid), worker_unit=claimed.worker_unit, task=claimed,
            )
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        _claim_coedit_paths(claimed.id, claimed.tenant)
        return True
    except Exception as exc:
        if _is_shared_launcher_prerequisite_fault(exc):
            _defer_for_shared_launcher_prerequisite(
                conn, claimed, exc, result, board=board,
            )
            return False
        if _record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


def _changes_requested_state(
    conn: sqlite3.Connection, task_id: str,
) -> tuple[int, Optional[int]]:
    row = conn.execute(
        "SELECT COUNT(*) AS rounds, MAX(id) AS latest_id FROM task_events "
        "WHERE task_id = ? AND kind = 'changes_requested' "
        "AND id > COALESCE(("
        "  SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'completed'"
        "), 0)",
        (task_id, task_id),
    ).fetchone()
    return int(row["rounds"]), (
        int(row["latest_id"]) if row["latest_id"] is not None else None
    )


def _manually_assigned_after(
    conn: sqlite3.Connection, task_id: str, event_id: Optional[int],
) -> bool:
    """Whether operator intent superseded the latest changes request."""
    if event_id is None:
        return False
    row = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'assigned' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or int(row["id"]) <= event_id:
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    source = str(payload.get("source") or "") if isinstance(payload, dict) else ""
    return not source.startswith("kanban.")


def _last_changes_requested_reason(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Reason text from the most recent ``changes_requested`` event, if any."""
    event = _kb._latest_event(conn, task_id, "changes_requested")
    if event is None:
        return None
    payload = _kb._json_dict(_kb._row_get(event, "payload"))
    reason = payload.get("reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def _apply_review_round_cap(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    changes_rounds: int,
    max_review_rounds: int,
    dry_run: bool,
) -> bool:
    """Hard-stop a card that hit ``kanban.max_review_rounds``: block it instead of
    re-dispatching to the implementer or the rework escalation profile.

    This is the hard stop for the review<->changes_requested loop (the reviewer-side
    round contract in the sdlc-review skill is advisory only). Mirrors
    :func:`_apply_rework_escalation`'s shape: a raw UPDATE guarded by the row's
    current status/assignee so a race (row already claimed/reassigned) is a no-op,
    plus a durable event carrying the round count and last reviewer reason.
    """
    if dry_run:
        return True
    reason = _last_changes_requested_reason(conn, task_id)
    try:
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL "
                "WHERE id = ? AND status = 'ready'",
                ("review_round_cap", task_id),
            )
            if cur.rowcount != 1:
                return False
            _kb._append_event(
                conn,
                task_id,
                "review_round_cap",
                {
                    "changes_rounds": changes_rounds,
                    "max_review_rounds": max_review_rounds,
                    "reason": reason,
                },
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply review round cap for task %s",
            task_id,
            exc_info=True,
        )
        return False
    return True


def _model_override_is_operator_set(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether the task's current model/provider/reasoning override was set by an
    operator, as opposed to the create-time routing classifier.

    An override is classifier-picked (and therefore clearable on escalation) ONLY
    when BOTH hold: (1) no ``model_override_set``/``reasoning_effort_set`` event
    exists for this task — those only ever come from an explicit
    ``kb.set_model_override``/``kb.set_reasoning_effort`` call (CLI `kanban
    set-model` or the dashboard PATCH), never from the dispatcher or the
    create-time classifier; and (2) the task's ``created`` event recorded
    ``route_source == "mechanical"`` — the one create-time routing decision that
    is NOT an operator choice (``kanban_model_routing.resolve_kanban_model_route``).
    Every other case (``route_source`` is ``"explicit"``, absent/legacy, or an
    operator later ran ``set-model``/``set-reasoning``) is operator-set and must
    survive rework escalation.
    """
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind IN ('model_override_set', 'reasoning_effort_set') LIMIT 1",
        (task_id,),
    ).fetchone() is not None:
        return True
    created = _kb._latest_event(conn, task_id, "created")
    route_source = _kb._json_dict(_kb._row_get(created, "payload")).get("route_source")
    return route_source != "mechanical"


def _apply_rework_escalation(
    conn: sqlite3.Connection,
    task_id: str,
    escalation_profile: str,
    *,
    previous_assignee: str,
    changes_rounds: int,
    dry_run: bool,
) -> bool:
    """Route repeated review rework to a specialist under its own model route.

    Preserves an operator-set model/provider/reasoning override across the
    handoff (see :func:`_model_override_is_operator_set`) — only a create-time
    routing-classifier pick is cleared, matching the specialist's OWN model
    defaults the way a classifier pick already did before an operator ever
    touched the card.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            row = conn.execute(
                "SELECT model_override, provider_override, reasoning_effort "
                "FROM tasks WHERE id = ? AND status = 'ready' AND assignee = ?",
                (task_id, previous_assignee),
            ).fetchone()
            if row is None:
                return False
            preserve = _model_override_is_operator_set(conn, task_id)
            if preserve:
                cur = conn.execute(
                    "UPDATE tasks SET assignee = ? "
                    "WHERE id = ? AND status = 'ready' AND assignee = ?",
                    (escalation_profile, task_id, previous_assignee),
                )
            else:
                cur = conn.execute(
                    "UPDATE tasks SET assignee = ?, model_override = NULL, "
                    "provider_override = NULL, reasoning_effort = NULL "
                    "WHERE id = ? AND status = 'ready' AND assignee = ?",
                    (escalation_profile, task_id, previous_assignee),
                )
            if cur.rowcount != 1:
                return False
            _kb._append_event(
                conn,
                task_id,
                "assigned",
                {
                    "assignee": escalation_profile,
                    "previous_assignee": previous_assignee,
                    "changes_rounds": changes_rounds,
                    "source": "kanban.review_rework_escalation_profile",
                    "previous_model_override": row["model_override"],
                    "previous_provider_override": row["provider_override"],
                    "previous_reasoning_effort": row["reasoning_effort"],
                    "preserved_overrides": preserve,
                },
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to escalate review rework for task %s to %r",
            task_id,
            escalation_profile,
            exc_info=True,
        )
        return False
    return True


def _review_row_implementer_owned(
    conn: sqlite3.Connection, task_id: str, row_assignee: str,
) -> bool:
    """True when a review-lane row is still owned by the profile that
    IMPLEMENTED it — the only state ``kanban.default_reviewer`` may touch.

    A bare ``row_assignee != default_reviewer`` inequality can't tell "still
    the implementer, never routed" apart from "explicitly routed to a real
    reviewer that just isn't the config's pick" — the latter must never be
    overridden, whether the routing came from ``kanban_request_review(
    reviewer=...)`` on the first pass or from ``_prior_reviewer`` provenance
    on a re-review. The latest ``review_requested`` event's ``reviewer``
    field records whether ``request_review`` made a handoff; the payload's
    ``implementer`` plus the current row assignee prove that the row is still
    owned by that implementer. A later operator/dashboard reassignment must
    win even when the original request left ``reviewer`` blank.
    """
    event = _kb._latest_event(conn, task_id, "review_requested")
    if event is None:
        # No provenance at all (e.g. a legacy row created before this event
        # existed) — nothing on record distinguishes implementer-owned from
        # explicitly-routed, so treat it as implementer-owned (today's
        # upgrade-safety behavior: the row is eligible for reassignment).
        return True
    payload = _kb._json_dict(_kb._row_get(event, "payload"))
    reviewer = payload.get("reviewer")
    if isinstance(reviewer, str) and reviewer.strip():
        return False
    implementer = payload.get("implementer")
    return isinstance(implementer, str) and bool(implementer.strip()) and row_assignee == implementer


def _apply_default_reviewer(
    conn: sqlite3.Connection, task_id: str, reviewer: str, *, previous_assignee: str, dry_run: bool,
) -> bool:
    """Reassign a review-lane row still owned by its implementer to ``reviewer``.

    Mirrors :func:`_apply_default_assignee`: mutates the row (not just the
    in-memory dispatch view) so board state stays honest — the card is now
    legitimately owned by the auto-assigned reviewer, not "assigned to the
    implementer but secretly routed elsewhere". The event payload records
    both sides of the handoff (``previous_assignee`` / ``reviewer`` /
    ``source``) so the audit trail shows implementer->reviewer provenance.

    This IS a cross-profile handoff exactly like an explicit
    ``kanban_request_review(reviewer=...)`` — the reassigned reviewer must
    run its own profile's model, never the implementer's pin. So
    ``model_override``/``provider_override``/``reasoning_effort`` are cleared
    here too (mirroring ``kanban_db.request_review``'s ``cross_profile``
    branch), and the implementer's values are snapshotted onto the SAME event
    this function already writes so ``request_changes`` can restore them on the
    round trip back (it reads the latest ``assigned`` event's
    ``implementer_model_override``/``implementer_provider_override`` the same
    way it reads a ``review_requested`` event's).

    ``dry_run`` reports without writing. Returns False when the write failed
    or the row was no longer a ``review`` row to reassign (status changed
    between read and write — never appends a phantom handoff event).
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            row = conn.execute(
                "SELECT model_override, provider_override, reasoning_effort FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            implementer_model_override = row["model_override"]
            implementer_provider_override = row["provider_override"]
            implementer_reasoning_effort = row["reasoning_effort"]
            cur = conn.execute(
                "UPDATE tasks SET assignee = ?, model_override = NULL, provider_override = NULL, "
                "reasoning_effort = NULL WHERE id = ? AND status = 'review'",
                (reviewer, task_id),
            )
            if cur.rowcount != 1:
                # Row left 'review' between read and write (claimed by
                # another dispatcher, reopened, etc.) — no handoff happened,
                # so no event should claim one did.
                return False
            payload: dict[str, Any] = {
                "assignee": reviewer,
                "previous_assignee": previous_assignee,
                "source": "kanban.default_reviewer",
            }
            if implementer_model_override is not None or implementer_provider_override is not None:
                payload["implementer_model_override"] = implementer_model_override
                payload["implementer_provider_override"] = implementer_provider_override
            if implementer_reasoning_effort is not None:
                payload["implementer_reasoning_effort"] = implementer_reasoning_effort
            _kb._append_event(conn, task_id, "assigned", payload)
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_reviewer=%r to task %s",
            reviewer, task_id, exc_info=True,
        )
        return False
    return True


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    result.reclaimed = _kb.release_stale_claims(conn)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = detect_crashed_workers(conn, board=board)
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.review_no_verdict.extend(getattr(detect_crashed_workers, "_last_review_no_verdict", []))
    result.interrupted.extend(getattr(detect_crashed_workers, "_last_interrupted", []))
    result.timed_out = enforce_max_runtime(conn)
    # Release serialization edges whose holder stalled, BEFORE promoting: a card
    # parked behind a now-blocked holder must be free to promote in this same
    # tick rather than waiting for a human to unblock a different card.
    result.released_coedit = _kc.release_stranded_coedit_edges(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker calls
    # kanban_complete/kanban_block or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee, tenant FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _any_spawnable_review(review_rows: list[sqlite3.Row]) -> bool:
    """Mirrors the review loop's own gate so human-pulled control-plane lanes
    don't tax ready throughput; assumes spawnable when profiles are unimportable."""
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        return any(row["assignee"] for row in review_rows)
    return any(row["assignee"] and profile_exists(row["assignee"]) for row in review_rows)


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile. When the
    profiles module isn't importable trust the operator's config: the
    downstream profile_exists check still buckets a missing profile as
    nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        try:
            from hermes_cli.profiles import profile_exists
            if not profile_exists(name):
                return None
        except Exception:
            pass
    return name


def _resolve_default_reviewer(default_reviewer: Optional[str]) -> Optional[str]:
    """``kanban.default_reviewer`` when it names a real, installed profile.

    Unlike :func:`_resolve_default_assignee` (which only fills a BLANK
    assignee, so trusting an unimportable ``profiles`` module is safe — the
    downstream ``profile_exists`` check in the dispatch lane still catches a
    bad name before spawn), this value OVERWRITES a real assignee. The same
    ``profiles`` import failure that disables THIS guard also disables that
    downstream safety net (``_profile_exists_fn`` returns ``None`` for the
    identical reason), so nothing would be left to catch a typo'd profile
    name replacing the implementer's — it would spawn a nonspawnable profile
    instead of falling back. Fail CLOSED here: an unimportable ``profiles``
    module or a profile that provably does not exist both resolve to
    ``None`` so the review loop falls back to the card's own assignee rather
    than overwriting it on unverified trust.
    """
    name = (default_reviewer or "").strip() or None
    if not name:
        return None
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        return None
    return name if profile_exists(name) else None


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    default_reviewer: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    dispatch_start_budget: Optional[int] = None,
    dispatch_start_window_seconds: int = 600,
    review_rework_escalation_profile: Optional[str] = None,
    max_review_rounds: Optional[int] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`.

    ``max_review_rounds``: ``None`` (the default every existing entry point
    passes) resolves from live config via :func:`resolve_dispatch_caps` so
    the round cap applies fleet-wide without every caller threading it
    explicitly; pass a concrete int (0 = unlimited) to override.
    """
    if max_review_rounds is None:
        max_review_rounds = resolve_dispatch_caps().max_review_rounds
    result = DispatchResult()
    # Sweep abandoned pre-task pasted-image uploads; best-effort — never abort the tick.
    try:
        _kb.reap_staged_attachments(board=board)
    except Exception:
        _kb._log.debug("reap_staged_attachments failed during dispatch tick", exc_info=True)
    # Durable provider pauses are resumed by the dispatcher itself, not an
    # external cron: this makes restart recovery deterministic and emits one
    # ``unblocked`` event via release_expired_provider_backoffs().
    _kb.release_expired_provider_backoffs(conn)
    _kb.clear_consumed_timeout_kill_intents(conn)
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans,
        board=board,
    )
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
    )

    start_budget = _positive_int_or_none(dispatch_start_budget)
    start_window = _positive_int(dispatch_start_window_seconds, 600)

    # A persisted integrity pause remains fail-closed. A normal start-budget
    # record is only an observable cooldown: the database event window remains
    # authoritative and, once capacity exists, this lock holder clears it before
    # a lane can claim a task.
    existing_pause = read_dispatch_pause(board)
    if existing_pause is not None:
        if existing_pause.get("reason") != "start_budget_exceeded":
            result.dispatch_paused = existing_pause
            return result
        if start_budget is None:
            if not dry_run:
                _clear_expired_start_budget_pause(board)
        else:
            recent_starts, next_eligible_at = _recent_dispatch_start_window(
                conn, window_seconds=start_window, budget=start_budget,
            )
            if recent_starts >= start_budget:
                result.dispatch_paused = _write_dispatch_pause(
                    board,
                    "start_budget_exceeded",
                    replace=True,
                    recent_starts=recent_starts,
                    budget=start_budget,
                    window_seconds=start_window,
                    next_eligible_at=next_eligible_at,
                ) if not dry_run else {
                    "reason": "start_budget_exceeded",
                    "recent_starts": recent_starts,
                    "budget": start_budget,
                    "window_seconds": start_window,
                    "next_eligible_at": next_eligible_at,
                }
                return result
            if not dry_run:
                _clear_expired_start_budget_pause(board)

    replay_ids = _terminal_card_replay_ids(conn)
    if replay_ids:
        result.dispatch_paused = {
            "reason": "terminal_card_replay",
            "task_ids": replay_ids,
        }
        if not dry_run:
            result.dispatch_paused = _write_dispatch_pause(
                board, "terminal_card_replay", task_ids=replay_ids,
            )
        return result

    if start_budget is not None:
        recent_starts, next_eligible_at = _recent_dispatch_start_window(
            conn, window_seconds=start_window, budget=start_budget,
        )
        if recent_starts >= start_budget:
            result.dispatch_paused = {
                "reason": "start_budget_exceeded",
                "recent_starts": recent_starts,
                "budget": start_budget,
                "window_seconds": start_window,
                "next_eligible_at": next_eligible_at,
            }
            if not dry_run:
                result.dispatch_paused = _write_dispatch_pause(
                    board,
                    "start_budget_exceeded",
                    recent_starts=recent_starts,
                    budget=start_budget,
                    window_seconds=start_window,
                    next_eligible_at=next_eligible_at,
                )
            return result

        # A single tick must not overshoot the sliding budget. The board is
        # rate limited immediately after consuming its final slot below.
        remaining_starts = start_budget - recent_starts
        spawn_budget = min(spawn_budget, remaining_starts) if spawn_budget is not None else remaining_starts

    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(review_rows):
        ready_budget = max(spawn_budget - 1, 0)
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = (
        count_running_tasks_by_assignee(conn, board) if per_profile_cap is not None else {}
    )
    # Co-edit guard. Built only when a ready row actually declares an edit
    # surface (or filed a hotspot), so a board that uses neither pays one cheap
    # query and behaves exactly as before.
    coedit_paths = _kc.edit_paths_for_tasks(conn, [row["id"] for row in ready_rows])
    coedit_index = (
        _kc.build_coedit_index(conn)
        if any(coedit_paths.values()) else None
    )
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        coedit_index=coedit_index, coedit_paths=coedit_paths,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    default_reviewer = _resolve_default_reviewer(default_reviewer)
    rework_escalation_profile = _resolve_default_reviewer(
        review_rework_escalation_profile
    )
    spawned = 0
    for row in ready_rows:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        # Hard round cap runs BEFORE escalation: a card that already hit the cap must block,
        # not get re-routed to a specialist for yet another round. Manual reassignment after
        # the last changes_requested is the same escape hatch the escalation path already
        # honors — an operator's explicit routing decision always wins over both mechanisms.
        changes_rounds, latest_change_id = _changes_requested_state(conn, row["id"])
        manually_reassigned = _manually_assigned_after(conn, row["id"], latest_change_id)
        if (
            max_review_rounds
            and changes_rounds >= max_review_rounds
            and not manually_reassigned
            and _apply_review_round_cap(
                conn, row["id"], changes_rounds=changes_rounds,
                max_review_rounds=max_review_rounds, dry_run=dry_run,
            )
        ):
            result.blocked_review_round_cap.append((row["id"], changes_rounds))
            continue
        if rework_escalation_profile and row_assignee != rework_escalation_profile:
            if (
                changes_rounds >= 2
                and not manually_reassigned
                and _apply_rework_escalation(
                    conn,
                    row["id"],
                    rework_escalation_profile,
                    previous_assignee=row_assignee,
                    changes_rounds=changes_rounds,
                    dry_run=dry_run,
                )
            ):
                result.auto_escalated_rework.append(
                    (row["id"], row_assignee, rework_escalation_profile, changes_rounds)
                )
                row_assignee = rework_escalation_profile
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
            spawned += 1
        if result.dispatch_paused is not None:
            return result

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            result.skipped_unassigned.append(row["id"])
            continue
        # kanban.default_reviewer: a review-lane card still owned by its
        # implementer never finds anything to do — the worker exits clean
        # (rc=0) and the dispatcher scores it a protocol_violation, parking
        # the card after failure_limit. When a different, real profile is
        # configured AND the row is still owned by its implementer (no
        # explicit reviewer= was ever routed for it), reassign the row
        # (mirrors default_assignee's mutate-the-row honesty) and dispatch
        # under the reviewer instead. Unset / same-as-assignee /
        # missing-profile / already-explicitly-routed all fall through
        # unchanged — never fail the tick over a misconfigured reviewer, and
        # never override a worker's own reviewer= choice (including one
        # re-routed by _prior_reviewer provenance on a re-review).
        if (
            default_reviewer
            and default_reviewer != row_assignee
            and _review_row_implementer_owned(conn, row["id"], row_assignee)
        ):
            if _apply_default_reviewer(
                conn, row["id"], default_reviewer,
                previous_assignee=row_assignee, dry_run=dry_run,
            ):
                result.auto_assigned_reviewer.append((row["id"], row_assignee, default_reviewer))
                row_assignee = default_reviewer
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="review", **lane_kwargs):
            spawned += 1
        if result.dispatch_paused is not None:
            return result

    if start_budget is not None and spawned and not dry_run:
        recent_starts, next_eligible_at = _recent_dispatch_start_window(
            conn, window_seconds=start_window, budget=start_budget,
        )
        if recent_starts >= start_budget:
            result.dispatch_paused = _write_dispatch_pause(
                board,
                "start_budget_exceeded",
                recent_starts=recent_starts,
                budget=start_budget,
                window_seconds=start_window,
                next_eligible_at=next_eligible_at,
            )
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _nonnegative_int(value: Any, default: int) -> int:
    """Parse a >= 0 int config value; ``None``/invalid/negative falls back to ``default``.

    Distinct from :func:`_positive_int` because 0 is a legitimate, meaningful value here
    (``kanban.max_review_rounds: 0`` = unlimited, an explicit operator choice) rather than
    something that should silently fall back to the default like an out-of-range cap would.
    """
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _positive_int_or_none(value: Any) -> Optional[int]:
    """Parse an optional positive-int cap; ``None`` when unset, invalid, or < 1.

    Distinct from :func:`_positive_int` because for a *cap*, "absent" and
    "zero" are not the same as "fall back to a default": ``None`` means
    unbounded and must stay distinguishable from a real number all the way
    into ``dispatch_once``.
    """
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package)."""
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then ``which("hermes")`` (Windows: safe PATH search,
    batch shims fall back to the module form), then ``sys.executable -m
    hermes_cli.main`` for shim-less environments (cron, systemd ``User=``,
    launchd). Mirrors ``gateway.run._resolve_hermes_bin``; local because
    ``hermes_cli`` sits below ``gateway`` in the dependency order.
    """
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


def _resolve_worker_run_analytics(task: Task, hermes_home: Optional[str]) -> dict[str, Optional[str]]:
    """Resolve the launch identity recorded on one run.

    Resolution is best-effort because an unreadable profile config must not
    prevent a worker from spawning. Card pins remain authoritative; otherwise
    the assignee profile's effective defaults are captured.
    """
    cfg: dict[str, Any] = {}
    if hermes_home:
        try:
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override
            from hermes_cli.config import load_config

            token = set_hermes_home_override(hermes_home)
            try:
                cfg = load_config() or {}
            finally:
                reset_hermes_home_override(token)
        except Exception as exc:
            _kb._log.debug(
                "kanban worker: could not resolve run analytics for HERMES_HOME=%r (%s)",
                hermes_home,
                exc,
            )

    raw_model_cfg = cfg.get("model")
    raw_agent_cfg = cfg.get("agent")
    model_cfg: dict[str, Any] = raw_model_cfg if isinstance(raw_model_cfg, dict) else {}
    agent_cfg: dict[str, Any] = raw_agent_cfg if isinstance(raw_agent_cfg, dict) else {}
    default_model = str(model_cfg.get("default") or "").strip() or None
    default_provider = str(model_cfg.get("provider") or "").strip() or None

    if task.model_override:
        model = str(task.model_override).strip() or None
        provider = str(task.provider_override or "").strip() or default_provider
        route_source = str(task.route_source or "").strip()
        model_source = (
            "routing"
            if route_source and route_source not in {"explicit", "default"}
            else "card_override"
        )
    else:
        model = default_model
        provider = default_provider
        model_source = "profile_default"

    reasoning = str(task.reasoning_effort or agent_cfg.get("reasoning_effort") or "").strip() or None
    return {
        "model": model,
        "provider": provider,
        "reasoning_effort": reasoning,
        "model_source": model_source,
    }


def _prepare_worker_launch(task: Task, hermes_home: Optional[str] = None) -> None:
    """Attach one stable session id and resolved launch identity to ``task``.

    The attributes are transient transport between the dispatch path and the
    environment builder. :func:`_stamp_worker_run_launch` persists them before
    process creation; the spawned-event writer then reads them from the run.
    Repeated calls are idempotent so the dispatch path and direct test helpers
    may both prepare the same task safely.
    """
    if not hermes_home and task.assignee:
        try:
            from hermes_cli.profiles import resolve_profile_env

            hermes_home = resolve_profile_env(task.assignee)
        except Exception:
            hermes_home = None
    if not getattr(task, "_worker_session_id", None):
        setattr(
            task,
            "_worker_session_id",
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
        )
    if not getattr(task, "_worker_run_analytics", None):
        setattr(task, "_worker_run_analytics", _resolve_worker_run_analytics(task, hermes_home))


def _stamp_worker_run_launch(conn: sqlite3.Connection, task: Task) -> None:
    """Persist launch identity before Popen so a fast worker cannot outrun it."""
    run_id = task.current_run_id or _kb._current_run_id(conn, task.id)
    if run_id is None:
        return
    analytics = dict(getattr(task, "_worker_run_analytics", {}) or {})
    session_id = getattr(task, "_worker_session_id", None)
    with _kb.write_txn(conn):
        conn.execute(
            """
            UPDATE task_runs
               SET session_id = ?, model = ?, provider = ?,
                   reasoning_effort = ?, model_source = ?
             WHERE id = ? AND ended_at IS NULL
            """,
            (
                session_id,
                analytics.get("model"),
                analytics.get("provider"),
                analytics.get("reasoning_effort"),
                analytics.get("model_source"),
                int(run_id),
            ),
        )


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        token = set_hermes_home_override(hermes_home)
        try:
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        finally:
            reset_hermes_home_override(token)
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            db.close()
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Opt this exact worker invocation into consuming the dispatcher-pinned
        # HERMES_SESSION_ID. An inherited env var alone must never make an
        # ordinary nested `hermes` command resume the parent worker session.
        "--use-env-session-id",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(hermes_home)
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])
    if task.goal_mode:
        # The kanban goal-loop hook only runs in cli.py's fully-quiet branch.
        # Without -Q the worker gets one turn, prints text, exits rc=0, and the
        # dispatcher records a protocol violation.
        cmd.append("-Q")
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    log_f = open(log_path, "ab")
    if task.current_run_id is not None:
        log_f.write(_kb.worker_log_run_marker(task.current_run_id).encode("utf-8"))
        log_f.flush()
    return log_f


def _worker_log_stamper_argv(log_path: Path) -> list[str]:
    """argv for the standalone per-line timestamp filter.

    Invoked by absolute script path with this interpreter, so it needs neither
    the ``hermes_cli`` package on ``PYTHONPATH`` nor an external binary.
    """
    from hermes_cli import kanban_log_stamp

    return [sys.executable, os.path.abspath(kanban_log_stamp.__file__), str(log_path)]


def _start_worker_log_stamper(
    task: Task, log_path: Path
) -> "Optional[tuple[Any, int]]":
    """Start the timestamp filter and return ``(proc, write_fd)``, or None.

    The worker's stdout/stderr is wired to ``write_fd`` instead of straight to
    the log file; the filter on the other end stamps each line and appends it.

    The filter is spawned through the SAME restart-safe path as the worker
    (own session, and its own transient systemd scope when this dispatcher is
    supervised). That is load-bearing, not defensive: left inside a supervised
    dispatcher's cgroup, ``systemctl restart`` would kill the filter, close the
    read end of the pipe, and SIGPIPE a live worker that was supposed to
    survive the restart.

    Returns None — and the caller falls back to today's byte-for-byte raw
    ``stdout=log_f`` spawn — whenever the filter cannot be established. A
    logging refinement must never stall the board or endanger a worker.
    """
    if task.current_run_id is None:
        # Mirrors _restart_safe_worker_argv: never mint an untraceable scope.
        return None
    try:
        argv = _worker_log_stamper_argv(log_path)
    except Exception:
        return None
    stamper_env = dict(os.environ)
    try:
        from tools.process_registry import restart_safe_supervised_child_argv

        argv = restart_safe_supervised_child_argv(
            argv,
            unit_suffix=f"kanban-log-{task.id}-run-{task.current_run_id}",
            env=stamper_env,
        )
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not place the worker-log timestamp filter for %s in a "
            "restart-safe scope (%s); logging this run without timestamps",
            task.id, exc,
        )
        return None
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        return None
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            argv,
            stdin=read_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=stamper_env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except Exception as exc:
        with contextlib.suppress(OSError):
            os.close(read_fd)
        with contextlib.suppress(OSError):
            os.close(write_fd)
        _kb._log.warning(
            "kanban: worker-log timestamp filter failed to start for %s (%s); "
            "logging this run without timestamps", task.id, exc,
        )
        return None
    # Only the filter needs the read end; holding a copy here would keep the
    # pipe from ever reaching EOF.
    with contextlib.suppress(OSError):
        os.close(read_fd)
    return proc, write_fd


def _restart_safe_worker_argv(
    task: Task,
    command: list[str],
    env: dict[str, str] | None = None,
    working_directory: str | None = None,
    service_environment: dict[str, str] | None = None,
) -> list[str]:
    """Wrap a worker spawned by a supervised systemd unit in the shared restart-safe scope.

    ``env`` is the child's environment, mutated in place with the user-bus variables the
    wrapped ``systemd-run --user`` needs — the dispatcher snapshots ``os.environ`` before
    this call, so without it the spawn execs systemd-run with no bus and dies instantly.
    """
    from tools.process_registry import restart_safe_supervised_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable scope.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        scoped = restart_safe_supervised_child_argv(
            command, unit_suffix=f"kanban-{task.id}-run-missing"
        )
        if scoped is not command:
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_supervised_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        env=env,
        working_directory=working_directory,
        service_environment=service_environment,
    )


def _worker_launcher_prefix() -> list[str]:
    """Resolve ``kanban.worker_launcher`` from config.

    Empty list (the default) means "no launcher" — every caller must treat
    that as a no-op and fall through to plain ``Popen``, keeping behaviour on
    Windows/macOS/non-systemd Linux byte-for-byte identical to today.

    Fails OPEN for a plain unresolvable binary (typo, uninstalled tool): log
    once and return ``[]`` so a misconfigured optional knob degrades to
    today's spawn behaviour instead of stalling the board. But for a
    ``systemd-run --user`` launcher specifically, this fails CLOSED against
    an unreachable user D-Bus: the gateway process's environment commonly
    lacks ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` even though the
    binary itself resolves fine via ``which()``, and letting that through
    means every spawn using this launcher fails at ``systemd-run`` time
    ("Failed to connect to bus: No medium found") instead of degrading —
    see :func:`_systemd_user_bus_reachable`. This is deliberately the
    opposite failure philosophy from ``restart_safe_gateway_child_argv``'s
    fail-closed ``RuntimeError``: that path guards an actual
    gateway-restart-survival contract for a narrow supervised case, while
    this one is plain opt-in operator config that must never stall the
    board either way — closed here means "don't use the launcher", not
    "refuse to spawn the worker".
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("kanban") or {}
    except Exception:
        return []
    raw = cfg.get("worker_launcher") or []
    if not isinstance(raw, (list, tuple)):
        return []
    prefix = [str(p) for p in raw if p]
    if not prefix:
        return []
    import shutil

    if shutil.which(prefix[0]) is None:
        _kb._log.warning(
            "kanban.worker_launcher binary %r not found on PATH; "
            "falling back to plain Popen spawn for this worker", prefix[0],
        )
        return []
    if os.path.basename(prefix[0]) == "systemd-run" and "--user" in prefix:
        if not _systemd_user_bus_reachable():
            return []
    return prefix


def _resolve_systemd_user_bus_env() -> "tuple[str, str]":
    """Resolve ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` for this
    process's own uid, falling back to the standard ``/run/user/<uid>``
    convention when the environment lacks them.

    Same derivation as hermes-dev-infrastructure PR #22's ``getent``-based
    playbook fix (which resolves a *different* user's uid via ``getent
    passwd`` before building the same paths); here we already run as the
    target uid, so ``os.getuid()`` replaces the ``getent`` lookup and the
    path convention is identical. Reuses ``hermes_cli.gateway``'s
    ``_runtime_dir_is_ours`` guard so a leaked ``XDG_RUNTIME_DIR`` from
    another user (e.g. a root shell where the env still points at
    ``/run/user/0``, #86558) is never trusted as our own bus directory.
    """
    from hermes_cli.gateway import _runtime_dir_is_ours

    uid = os.getuid()  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    fallback = f"/run/user/{uid}"
    if not xdg or not _runtime_dir_is_ours(xdg):
        xdg = fallback if _runtime_dir_is_ours(fallback) else (xdg or fallback)
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={xdg}/bus"
    return xdg, bus


def _is_systemd_user_scope_prefix(prefix: list[str]) -> bool:
    """True if *prefix* is a ``systemd-run --user`` (optionally ``--scope``) launcher entry."""
    return bool(prefix) and os.path.basename(prefix[0]) == "systemd-run" and "--user" in prefix


def _worker_launcher_env_overrides(prefix: list[str]) -> dict[str, str]:
    """Extra env vars a resolved ``kanban.worker_launcher`` prefix needs on the spawned child.

    ``_systemd_user_bus_reachable`` resolves ``XDG_RUNTIME_DIR``/
    ``DBUS_SESSION_BUS_ADDRESS`` to decide whether a ``systemd-run --user``
    launcher entry is usable, but ``systemd-run --user`` itself finds the
    bus via its OWN process environment at spawn time — resolving the
    values for the reachability check and then discarding them meant the
    check could pass (the uid fallback resolves a real, reachable socket)
    while the actual spawn still failed with "Failed to connect to bus:
    No medium found", because the child's environment (built by
    ``build_subprocess_env``, which may already have stripped these vars)
    never received them. Only fires for a ``systemd-run --user`` launcher
    entry; every other launcher configuration (or the default ``[]``) is
    untouched.
    """
    if not _is_systemd_user_scope_prefix(prefix):
        return {}
    xdg, bus = _resolve_systemd_user_bus_env()
    return {"XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus}


def _cmd_is_systemd_user_scope_wrapped(command: list[str]) -> bool:
    """True if *command* is a systemd-run user scope invocation.

    ``systemd-run --pipe`` implicitly creates a scope on current systemd, so
    recognize that form as well as an explicit ``--scope``.  The shared
    restart-safe helper deliberately uses ``--pipe`` to preserve the child's
    stdio, and its unit is still registered as ``.scope``.
    """
    if not command or os.path.basename(command[0]) != "systemd-run":
        return False
    return "--user" in command and ("--scope" in command or "--pipe" in command)


def _extract_unit_from_systemd_scope_argv(command: list[str]) -> Optional[str]:
    """Recover the registered unit id from a systemd-run user invocation.

    Explicit ``--scope`` registers ``.scope``; the restart-safe helper's
    ``--pipe`` form instead registers a transient ``.service``.  Persist the
    suffix systemd will actually create so later ``systemctl --user`` calls
    address the same live unit.
    """
    suffix = ".scope" if "--scope" in command else ".service"
    for i, part in enumerate(command):
        if part == "--unit" and i + 1 < len(command):
            value = command[i + 1]
            return value if value.endswith((".scope", ".service")) else f"{value}{suffix}"
        if part.startswith("--unit="):
            value = part[len("--unit="):]
            return value if value.endswith((".scope", ".service")) else f"{value}{suffix}"
    return None


_SYSTEMD_USER_BUS_WARNED = False


def _systemd_user_bus_reachable() -> bool:
    """True if the resolved user D-Bus socket actually exists on disk.

    Fails CLOSED: a ``kanban.worker_launcher`` entry that shells out to
    ``systemd-run --user`` must never be handed to ``Popen`` when the bus is
    unreachable — the prior guard checked only ``shutil.which()``, which
    passes even though the gateway's stripped environment commonly has no
    ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS``, so enabling the knob as
    documented let ``systemd-run --user`` fail at spawn time instead of
    degrading to a plain Popen spawn. Logs once per process, not once per
    spawn, so a persistently-unreachable bus doesn't spam the log every tick.
    """
    global _SYSTEMD_USER_BUS_WARNED
    xdg, bus = _resolve_systemd_user_bus_env()
    socket_path = bus[len("unix:path="):] if bus.startswith("unix:path=") else os.path.join(xdg, "bus")
    if socket_path and os.path.exists(socket_path):
        return True
    if not _SYSTEMD_USER_BUS_WARNED:
        _kb._log.warning(
            "kanban.worker_launcher configures a systemd-run --user spawn but no "
            "reachable user D-Bus socket was found (checked %s); falling back to "
            "plain Popen spawn for this worker until the bus becomes reachable",
            socket_path or "<unresolved>",
        )
        _SYSTEMD_USER_BUS_WARNED = True
    return False


def _worker_launcher_unit_name(task: Task) -> str:
    """``--unit=`` value for a launcher-wrapped worker; stable across the task's runs.

    Always carries the explicit ``.scope`` suffix: every later
    ``systemctl --user`` query/stop against this unit must use the exact
    same string ``systemd-run`` registered it under, or it silently
    resolves to a same-named ``.service`` unit that never existed
    (``rc=5``/"not loaded", worker left running).
    """
    run_part = task.current_run_id if task.current_run_id is not None else "missing"
    return f"kanban-{task.id}-run-{run_part}.scope"


def _apply_worker_launcher(task: Task, command: list[str]) -> "tuple[list[str], Optional[str]]":
    """Prepend ``kanban.worker_launcher`` (when configured and resolvable) to *command*.

    Returns ``(argv, worker_unit)``. ``worker_unit`` is the unit name minted
    via an appended ``--unit=<name>`` flag when the launcher fired, else
    ``None`` — the default/no-launcher path, where ``argv`` is ``command``
    unchanged. The operator supplies only the launcher binary + its own
    flags in config; the dispatcher appends ``--unit=<name>`` and the
    trailing ``-- <command>`` itself so every launcher invocation carries a
    traceable, task-scoped unit name without the operator hand-typing it.

    Double-scope guard: in the supervised-gateway topology,
    ``_restart_safe_worker_argv`` may have already wrapped *command* in its
    own ``systemd-run --user --scope``. If the resolved launcher prefix is
    ALSO a ``systemd-run --user --scope`` invocation, nesting a second one
    around the first is not a stronger wrap — ``--scope`` is a transparent
    exec, so the outer ``systemd-run`` execs straight into the inner one
    and only the INNER unit ever registers with systemd; the outer
    (persisted) unit name would be permanently unresolvable
    (``LoadState=not-found``), defeating every termination path that
    depends on ``tasks.worker_unit`` actually existing. In that case, skip
    the redundant outer wrap and track the ALREADY-REAL inner unit instead
    — the worker is still isolated in exactly one scope, and
    ``worker_unit`` still refers to a unit that genuinely exists.
    """
    prefix = _worker_launcher_prefix()
    if not prefix:
        return command, None
    if _is_systemd_user_scope_prefix(prefix) and _cmd_is_systemd_user_scope_wrapped(command):
        return command, _extract_unit_from_systemd_scope_argv(command)
    unit_name = _worker_launcher_unit_name(task)
    argv = [*prefix, f"--unit={unit_name}", "--", *command]
    return argv, unit_name


def _default_spawn(task: Task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env(
        scrub_secrets=is_multiplex_active(),
        inherit_profile_home=True,
    )
    # The dispatcher is detached from every conversation; its worker must never
    # inherit routing mirrored by a previous gateway turn.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        pass
    _prepare_worker_launch(task, env.get("HERMES_HOME"))
    env["HERMES_SESSION_ID"] = str(getattr(task, "_worker_session_id"))
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    # Vouch for the pins above: names the kanban home they were computed under.
    # They normally resolve INSIDE that home and need no vouching, but symlink /
    # Docker layouts can put the board outside the home the worker resolves, and
    # without this the containment guard in kanban_db._pin_is_honored() would
    # drop a legitimate pin. A worker (or a probe it writes) that re-declares
    # HERMES_HOME/HERMES_KANBAN_HOME makes this stamp disagree, so its sandbox
    # is honored instead of the production pin — see kanban_db._board_path().
    env[_kb.KANBAN_PIN_HOME_ENV] = str(_kb.kanban_home())
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    # `--cli` is the highest-precedence TUI override; dropping HERMES_TUI covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))
    # A worker spawned by a supervised systemd unit must leave that unit's cgroup before
    # startup; otherwise restarting the service kills the worker mid-task. ``env`` is
    # passed so the scope wrapper can add the user-bus vars it needs to reach systemd.
    service_environment = {
        key: value
        for key, value in env.items()
        if key in {
            "HERMES_HOME", "HERMES_TENANT", "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_WORKSPACE", "HERMES_SESSION_SOURCE", "HERMES_SESSION_ID", "TERMINAL_CWD",
            "HERMES_KANBAN_BRANCH", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS", "TERMINAL_TIMEOUT",
            "TERMINAL_MAX_FOREGROUND_TIMEOUT", "HERMES_KANBAN_DB", "HERMES_KANBAN_PIN_HOME",
            "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD", "HERMES_PROFILE",
        }
    }
    cmd = _restart_safe_worker_argv(
        task,
        cmd,
        env,
        workspace if os.path.isdir(workspace) else None,
        service_environment,
    )
    # A supervised dispatcher already creates a real restart-safe scope even
    # with worker_launcher=[]; retain its exact registered name for later
    # reaping.  A configured launcher may replace that scope with its own,
    # except when it recognizes the existing systemd scope and returns it.
    restart_safe_unit = (
        _extract_unit_from_systemd_scope_argv(cmd)
        if _cmd_is_systemd_user_scope_wrapped(cmd)
        else None
    )
    # Apply the optional configured launcher after the restart-safe wrapper. A
    # systemd scope prefix recognizes an existing scope and preserves its real
    # unit instead of nesting a non-existent outer unit.
    prefix = _worker_launcher_prefix()
    cmd, launcher_unit = _apply_worker_launcher(task, cmd)
    task.worker_unit = launcher_unit or restart_safe_unit
    env.update(_worker_launcher_env_overrides(prefix))
    log_f = _open_worker_log(task, board)
    # Per-line wall-clock timestamps: the worker writes into a pipe whose other
    # end is a standalone filter process that stamps each line and appends it to
    # the same log file. ``stamper`` is None when the filter could not be
    # started, in which case the worker's fd goes straight to the log exactly as
    # it did before timestamps existed.
    stamper = _start_worker_log_stamper(task, Path(log_f.name))
    worker_stdout = stamper[1] if stamper else log_f
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=worker_stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        if stamper:
            with contextlib.suppress(OSError):
                os.close(stamper[1])
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    if stamper:
        # The worker now owns the only writing end of the pipe; this copy must
        # go or the filter never sees EOF and never exits. ``log_f`` likewise:
        # with the filter appending, this process holding the file open serves
        # nothing.
        with contextlib.suppress(OSError):
            os.close(stamper[1])
        log_f.close()
    # Intentionally NOT closing log_f in the un-stamped path: the child keeps
    # writing after return; the OS-level FD stays open in the child until it
    # exits. The stamped path preserves that survival property through the
    # filter process, which is spawned into its own session/scope for exactly
    # this reason.
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    board: Optional[str] = None,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks.

    Each tick resolves the caps through :func:`resolve_dispatch_caps`, the same
    helper the gateway tick, ``hermes kanban dispatch`` and the dashboard nudge
    use. Resolving only ``max_in_progress`` here (as this loop used to) left
    ``max_in_progress_per_profile`` as ``None``, and ``dispatch_once`` reads an
    omitted cap as *unlimited* — so the standalone daemon could hand one
    profile its entire backlog while every other entry point held it to the
    configured per-profile limit. The caps bound the HOST, not an entry point.
    """
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            caps = resolve_dispatch_caps()
            with contextlib.closing(_kbc.connect(board=board)) as conn:
                res = dispatch_once(
                    conn,
                    board=board,
                    max_spawn=max_spawn if max_spawn is not None else caps.max_spawn,
                    max_in_progress=caps.max_in_progress,
                    max_in_progress_per_profile=caps.max_in_progress_per_profile,
                    default_assignee=caps.default_assignee,
                    default_reviewer=caps.default_reviewer,
                    dispatch_start_budget=caps.dispatch_start_budget,
                    dispatch_start_window_seconds=caps.dispatch_start_window_seconds,
                    review_rework_escalation_profile=caps.review_rework_escalation_profile,
                    max_review_rounds=caps.max_review_rounds,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_coedit as _kc  # noqa: E402
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_workspace as _kbw  # noqa: E402
