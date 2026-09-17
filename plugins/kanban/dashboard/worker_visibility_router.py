"""Kanban dashboard — worker visibility: active-worker list, per-run inspect/terminate."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from hermes_cli import kanban_db

from plugins.kanban.dashboard._common import (
    _BOARD_Q_DESCRIPTION,
    _board_conn,
    _conflict,
    _require_run,
)

_BOARD_Q = Query(None, description=_BOARD_Q_DESCRIPTION)

try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]


router = APIRouter()


@router.get("/workers/active")
def list_active_workers(board: Optional[str] = _BOARD_Q):
    """Every running worker: an open ``task_runs`` row with a ``worker_pid`` whose
    task is ``running``. Returns ``{workers, count, checked_at}``."""
    with _board_conn(board) as (board, conn):
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, t.title AS task_title, t.status AS task_status, "
            "t.assignee AS task_assignee, r.profile, r.worker_pid, r.started_at, r.claim_lock, "
            "r.claim_expires, r.last_heartbeat_at, r.max_runtime_seconds "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.ended_at IS NULL AND r.worker_pid IS NOT NULL AND t.status = 'running' "
            "ORDER BY r.started_at ASC").fetchall()
        workers = [dict(row) for row in rows]
        return {"workers": workers, "count": len(workers), "checked_at": int(time.time())}


@router.get("/runs/{run_id}")
def get_run_endpoint(run_id: int, board: Optional[str] = _BOARD_Q):
    """``{run: {...}}`` with the same serialisation as ``GET /tasks/{id}``; 404 if unknown."""
    with _board_conn(board) as (board, conn):
        return {"run": asdict(_require_run(conn, run_id))}


@router.get("/runs/{run_id}/inspect")
def inspect_run_endpoint(run_id: int, board: Optional[str] = _BOARD_Q):
    """Live psutil stats for a run's worker; ``{alive: false, reason}`` when unavailable and
    access-denied reported inline rather than as a 500."""
    with _board_conn(board) as (board, conn):
        r = _require_run(conn, run_id)

    def _dead(reason: str, **extra) -> dict:
        return {"run_id": run_id, "alive": False, **extra, "reason": reason}

    if r.ended_at is not None:
        return _dead("run already ended")
    pid = r.worker_pid
    if pid is None:
        return _dead("no worker_pid recorded")
    if _psutil is None:
        return _dead("psutil not available", pid=pid)
    try:
        proc = _psutil.Process(pid)
        info = proc.as_dict(attrs=["cpu_percent", "memory_info", "num_threads", "status", "create_time", "cmdline"])
        try:
            num_fds = proc.num_fds()
        except AttributeError:  # POSIX-only
            num_fds = None
        mem = info.get("memory_info")
        return {
            "run_id": run_id, "alive": True, "pid": pid,
            "cpu_percent": info.get("cpu_percent"),
            "memory_rss_bytes": mem.rss if mem else None,
            "memory_vms_bytes": mem.vms if mem else None,
            "num_threads": info.get("num_threads"), "num_fds": num_fds,
            "status": info.get("status"), "create_time": info.get("create_time"), "cmdline": info.get("cmdline")}
    except _psutil.NoSuchProcess:
        return _dead("process not found", pid=pid)
    except _psutil.AccessDenied:
        return {"run_id": run_id, "alive": True, "pid": pid, "error": "access denied"}


class TerminateRunBody(BaseModel):
    reason: Optional[str] = None


@router.post("/runs/{run_id}/terminate")
def terminate_run_endpoint(run_id: int, payload: TerminateRunBody, board: Optional[str] = _BOARD_Q):
    """Terminate an in-flight run via ``reclaim_task`` (same SIGTERM->SIGKILL flow, bookkeeping
    and events as ``POST /tasks/{id}/reclaim``); 409 if already ended / not reclaimable.

    Closes the gap left by PR #28432, which shipped the read-only sibling endpoints (``/workers/active``,
    ``/runs/{run_id}``, ``/runs/{run_id}/inspect``) but no termination control surface.
    """
    with _board_conn(board) as (board, conn):
        r = _require_run(conn, run_id)
        if r.ended_at is not None:
            raise _conflict(f"run {run_id} already ended")
        if not kanban_db.reclaim_task(conn, r.task_id, reason=payload.reason):
            raise _conflict(f"cannot terminate run {run_id}: task {r.task_id} is no longer in a reclaimable state")
        return {"ok": True, "run_id": run_id, "task_id": r.task_id}
