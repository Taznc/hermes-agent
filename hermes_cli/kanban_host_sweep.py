"""Host-level PID sweep: find live kanban-worker-shaped processes that no
task row on any board claims (via ``worker_pid``).

This is the durable half of the ``delete_task`` orphan fix (t_749b0510):
that fix closes the ONE deletion path that runs through ``delete_task``, but
every existing per-row reclaim function (``detect_crashed_workers``,
``reconcile_orphaned_running``, ``count_running_tasks``, ...) starts its
query from ``WHERE status='running'`` on a *row*. A row dropped by any other
path (raw SQL repair, a DB restore, a future bulk-delete script, manual
``sqlite3`` surgery) is invisible to all of them by construction, and its
live worker process just keeps running forever with no task to report back
to.

This module inverts the direction: start from the live PROCESS table, filter
to processes that look like kanban workers, and check each one against every
board's ``tasks.worker_pid`` column. A PID with no claiming row anywhere is
an orphan.

Deliberately NOT a modification of the existing per-row reclaim functions —
they intentionally start from the row and stay that way. Deliberately does
NOT kill the orphan process; detection only. See ``cron/AGENTS.md`` for the
kanban board/dispatcher invariants this sweep must not violate (never infer
process identity from argv substrings; use a real regex against the worker's
own argv shape, not a loose keyword).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc
from hermes_cli.kanban_db_dispatch import _pid_alive

_log = logging.getLogger(__name__)

# A kanban worker's argv always carries this literal marker (see
# ``kanban_db_dispatch._worker_argv``: ``chat -q "work kanban task <id>"``).
# Anchored on the marker + a real task id shape, never a loose substring like
# ``"kanban" in cmdline`` (root AGENTS.md: never infer process identity from
# argv substrings).
_WORKER_TASK_RE = re.compile(r"work kanban task (t_[0-9a-f]+)")

# Name of the durable JSONL sidecar recording detected orphans. Not a board
# artifact (an orphan may not resolve to ANY board once its row is gone), so
# it lives at the shared kanban home root rather than inside one board's DB.
_ORPHAN_LOG_NAME = "kanban_orphan_workers.jsonl"


def _read_process_cmdline(pid: int) -> Optional[str]:
    """Best-effort process command line; delegates to the canonical reader
    shared with the gateway's process-identity checks."""
    from gateway.status import _read_process_cmdline as _reader
    return _reader(pid)


def _iter_pids() -> "list[int]":
    """All PIDs currently visible on this host. Linux: ``/proc``. Other
    platforms: best-effort via ``psutil`` (soft dependency; empty on failure
    rather than raising, so a missing/broken psutil silences the sweep
    instead of crashing the dispatcher)."""
    if os.path.isdir("/proc"):
        pids: "list[int]" = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                pids.append(int(entry))
        return pids
    try:
        import psutil  # type: ignore
        return list(psutil.pids())
    except Exception:
        return []


def _live_kanban_worker_pids() -> "dict[int, str]":
    """``{pid: cmdline}`` for every live process whose argv matches the
    kanban-worker task marker. A dead/zombie pid observed mid-scan (TOCTOU:
    the process table changes under us) is dropped rather than reported."""
    found: "dict[int, str]" = {}
    my_pid = os.getpid()
    for pid in _iter_pids():
        if pid == my_pid:
            continue
        cmdline = _read_process_cmdline(pid)
        if not cmdline or not _WORKER_TASK_RE.search(cmdline):
            continue
        if not _pid_alive(pid):
            continue
        found[pid] = cmdline
    return found


def _task_id_from_cmdline(cmdline: str) -> Optional[str]:
    m = _WORKER_TASK_RE.search(cmdline)
    return m.group(1) if m else None


def _find_claiming_task(pid: int, *, host_prefix: str) -> "Optional[tuple[str, str]]":
    """``(board_slug, task_id)`` claiming ``pid`` via ``worker_pid`` on THIS
    host, across every board, or ``None`` if no row claims it.

    Host-scoped: a claim_lock from a different host would name the same pid
    number for an unrelated process, so only locks carrying this host's
    prefix (``kanban_db._host_prefix()``, mirroring ``detect_crashed_workers``
    / ``_terminate_reclaimed_worker``) count as a match.
    """
    try:
        boards = _kb.list_boards(include_archived=True)
    except Exception:
        _log.warning("kanban orphan sweep: could not enumerate boards", exc_info=True)
        raise

    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            db_path = _kb.kanban_db_path(board=slug).expanduser()
            if not db_path.exists():
                continue
            conn = _kbc.connect(board=slug)
        except Exception:
            continue
        try:
            row = conn.execute(
                "SELECT id FROM tasks WHERE worker_pid = ? AND claim_lock LIKE ? LIMIT 1",
                (pid, f"{host_prefix}%"),
            ).fetchone()
            if row is not None:
                return (slug, row["id"])
        except Exception:
            continue
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    return None


def _orphan_log_path() -> Path:
    return _kb.kanban_home() / _ORPHAN_LOG_NAME


def _record_orphans(orphans: "list[dict]") -> None:
    """Append one JSON line per detected orphan to a durable sidecar file.

    A structured, on-disk, append-only record survives process restarts and
    is greppable/parseable by an ops path — unlike a bare log line, which is
    only as durable as whatever log-rotation policy happens to be configured
    for this process. No board to attach an event to: the whole point of an
    orphan is that its task row (and therefore its board membership) is
    gone, so this cannot live inside any one board's ``task_events`` table.
    """
    path = _orphan_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for entry in orphans:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        _log.warning("kanban orphan sweep: failed to record orphan(s)", exc_info=True)


def sweep_orphaned_worker_pids(*, record: bool = True) -> "list[dict]":
    """Host-level orphan detection sweep.

    Enumerates live worker-shaped processes on this host, resolves each to a
    claiming ``(board, task_id)`` via ``worker_pid`` across every board's DB,
    and returns the ones with no claiming row anywhere — i.e. their task row
    is gone (or was never theirs) while the process is still alive.

    Fails CLOSED on a board-enumeration error: if boards can't be listed at
    all, returns ``[]`` rather than risk flagging every live worker as
    orphaned (a false-positive storm is worse than a missed sweep this
    tick — the next tick tries again).

    Does not kill or otherwise touch the orphan process; detection only.
    When ``record`` is True (the default), each orphan is appended to the
    durable sidecar log (:func:`_record_orphans`) so a human/ops path
    notices even if nobody is watching the return value live.
    """
    candidates = _live_kanban_worker_pids()
    if not candidates:
        return []

    host_prefix = _kb._host_prefix()
    orphans: "list[dict]" = []
    now = int(time.time())
    for pid, cmdline in candidates.items():
        try:
            claim = _find_claiming_task(pid, host_prefix=host_prefix)
        except Exception:
            # Board enumeration itself failed: abort the whole sweep rather
            # than report partial/misleading results for this tick.
            return []
        if claim is not None:
            continue
        orphans.append({
            "pid": pid,
            "cmdline": cmdline[:1000],
            "task_id_from_argv": _task_id_from_cmdline(cmdline),
            "detected_at": now,
            "host_prefix": host_prefix,
        })

    if orphans and record:
        _record_orphans(orphans)
    return orphans
