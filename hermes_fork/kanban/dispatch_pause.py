"""Per-board Kanban dispatch pause (tier T1, see FORK.md).

Upstream ``next`` has ONE pause: the global emergency stop (``hermes pause`` /
``/pause``, ``agent/estop.py``). It halts cron, new gateway turns and the
dispatcher on every board together. Operators also need a narrower switch:
stop claiming new Kanban work on one board, or on every board, so running
workers can drain before a restart while cron and chat keep working. This
module is that switch. It ports dev's board pause (95bea51183 and its
``kanban_db_dispatch`` helpers) without dev's inline edits to upstream's
dispatcher and dashboard API.

State is a sentinel file beside the board's database,
``<kanban.db stem>.dispatch-pause.json``, in dev's format
(``{"reason": "operator_paused", "paused_at": <epoch>, "paused_by", "note"}``),
so a board carried over from a dev home keeps its pause. Any sentinel counts
as paused, including dev's self-expiring ``start_budget_exceeded`` records.
Next never writes those (its start budget is derived from events, see
``start_budget``), so an operator clears one with resume. Deriving the path
from the DB path keeps ``HERMES_KANBAN_DB`` pins isolated: a sandboxed worker
or test can never pause or resume the live board. An unreadable or malformed
sentinel still counts as paused (fail closed), because treating damaged
safety state as absent would widen dispatch.

Enforcement: :func:`admit`, called from the existing ``start-budget`` FORK
ANCHOR through :func:`hermes_fork.kanban.start_budget.admit`, so it adds no
upstream lines. It runs under upstream's per-board ``_dispatch_tick_lock``
before any row is claimed, so a paused board claims and spawns nothing.
Running workers are never touched, and the reclaim of dead or stale workers
still runs.

:func:`pause` takes the same tick lock (bounded wait) before writing. Once it
reports ``paused``, no tick that started earlier can still spawn. If a tick
holds the lock past the wait it reports ``busy`` and writes nothing.

The routes live in the T0 ``fork-kanban`` dashboard plugin
(``hermes-customizations/plugins/fork-kanban``), which calls this module.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SUFFIX = ".dispatch-pause.json"
REASON = "dispatch_paused"
OPERATOR_PAUSED = "operator_paused"
UNREADABLE = "pause_state_unreadable"
LOCK_WAIT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.05


def pause_path_for_db(db_path: Path) -> Path:
    return Path(db_path).with_suffix(SUFFIX)


def pause_path(board: Optional[str] = None) -> Path:
    """Sentinel path for ``board`` (None = the current board)."""
    from hermes_cli import kanban_db as _kb

    return pause_path_for_db(_kb.kanban_db_path(board=board))


def _read(path: Path) -> Optional[dict[str, Any]]:
    """``None`` when not paused, else the pause record (fail closed on damage).

    Read directly, never ``Path.exists()`` first: Python 3.14 maps an
    inaccessible parent's PermissionError to ``False`` there, which would
    report an existing sentinel as absent. Only FileNotFoundError is absent.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return {"reason": UNREADABLE, "detail": str(exc), "path": str(path)}
    if not isinstance(raw, dict) or not raw.get("reason"):
        return {"reason": UNREADABLE, "detail": "pause state must be an object with a reason", "path": str(path)}
    return raw


def read_state(board: Optional[str] = None) -> Optional[dict[str, Any]]:
    return _read(pause_path(board))


def is_paused(board: Optional[str] = None) -> bool:
    return read_state(board) is not None


def _db_path_of(conn: sqlite3.Connection) -> Optional[Path]:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def admit(conn: sqlite3.Connection, result: Any, may_spawn: bool) -> bool:
    """Tick admission: ``False`` (claim nothing) while this board is paused.

    The board is the database the tick runs on, so this never disagrees with
    ``HERMES_KANBAN_DB`` pins. Held rows are reported in
    ``result.respawn_guarded`` as ``dispatch_paused``, so the dispatcher's
    "stuck" warning and ``hermes kanban dispatch`` name the reason.
    """
    if not may_spawn:
        return may_spawn
    db_path = _db_path_of(conn)
    if db_path is None or _read(pause_path_for_db(db_path)) is None:
        return may_spawn
    from hermes_cli import kanban_db_dispatch as _kbd

    lanes = ["ready"] + (["review"] if _kbd.review_dispatch_enabled() else [])
    for lane in lanes:
        for row in _kbd._lane_rows(conn, lane):
            if row["assignee"]:
                result.respawn_guarded.append((row["id"], REASON))
    return False


def _acquire_tick_lock(db_path: Path, wait_seconds: float):
    """The board's dispatcher tick lock as an open handle, or None if a tick kept it.

    Same file and primitive as upstream's ``_dispatch_tick_lock``, held only
    for the sentinel write. Without a locking primitive the handle is returned
    unlocked, the same way upstream degrades.
    """
    from hermes_cli import kanban_db_connect as _kbc

    lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        try:
            if _kbc._try_lock_nb(handle):
                return handle
        except (OSError, AttributeError):
            return handle
        if time.monotonic() >= deadline:
            handle.close()
            return None
        time.sleep(_LOCK_POLL_SECONDS)


def _release(handle) -> None:
    from hermes_cli import kanban_db as _kb

    with contextlib.suppress(OSError, AttributeError, ImportError):
        if _kb._IS_WINDOWS:
            import msvcrt

            handle.seek(0)
            getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def pause(
    board: Optional[str] = None,
    *,
    note: Optional[str] = None,
    paused_by: Optional[str] = None,
    wait_seconds: float = LOCK_WAIT_SECONDS,
) -> dict[str, Any]:
    """Pause new dispatch on ``board``. Never kills a worker.

    Returns ``{"paused": True, "busy": False, "state"}``, or
    ``{"paused": <already paused>, "busy": True, "state"}`` when a dispatch
    tick held the board's lock for longer than ``wait_seconds``. Re-pausing a
    paused board rewrites the record (new note/time).
    """
    from hermes_cli import kanban_db as _kb

    db_path = _kb.kanban_db_path(board=board)
    path = pause_path_for_db(db_path)
    handle = _acquire_tick_lock(db_path, wait_seconds)
    if handle is None:
        current = _read(path)
        return {"paused": current is not None, "busy": True, "state": current}
    try:
        state: dict[str, Any] = {"reason": OPERATOR_PAUSED, "paused_at": int(time.time())}
        if paused_by:
            state["paused_by"] = paused_by
        if note and note.strip():
            state["note"] = note.strip()
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
    finally:
        _release(handle)
    _kb._log.info("kanban dispatch paused on board %s (%s)", board or "<current>", path)
    return {"paused": True, "busy": False, "state": state}


def resume(board: Optional[str] = None) -> dict[str, Any]:
    """Clear ``board``'s pause. ``was_paused`` is False when nothing was set."""
    from hermes_cli import kanban_db as _kb

    path = pause_path(board)
    try:
        path.unlink()
    except FileNotFoundError:
        return {"resumed": True, "was_paused": False}
    _kb._log.info("kanban dispatch resumed on board %s (%s)", board or "<current>", path)
    return {"resumed": True, "was_paused": True}
