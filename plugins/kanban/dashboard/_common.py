"""Shared plumbing for the kanban dashboard plugin's extracted routers.

Every sibling router (``attachments_router``, ``worker_visibility_router``,
``recovery_router``, ``dispatch_pause_router``, ``boards_router``) imports its
board/connection/error-mapping helpers from here instead of duplicating them.
``plugin_api.py`` re-exports the same names for backward call-site compat within
this package and mounts each sibling's ``router`` onto its own facade ``router``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from functools import partial
from typing import Any, Callable, Iterator, Optional

from fastapi import HTTPException

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc

_BOARD_Q_DESCRIPTION = "Kanban board slug (omit for current)"

# Dashboard columns, left-to-right ("archived" is a filter toggle, not a column). Keep in
# sync with kanban_db.VALID_STATUSES — a status missing here gets mis-bucketed into ``todo``.
# ``on_hold`` is the human-initiated shelf/pause column — distinct from ``blocked`` (worker needs
# input) and ``scheduled`` (waiting on time). ``idea``/``roadmap`` are the inert wishlist lanes:
# because the board reads left-to-right as a lifecycle: a wish is captured (idea), hashed out
# (roadmap), and only then authorized into the work queue that starts at ``triage``.
BOARD_COLUMNS: list[str] = [
    "idea", "roadmap",
    "triage", "todo", "scheduled", "ready", "running", "blocked", "on_hold", "review", "done",
]


def _normalize_slug_or_400(slug: str) -> Optional[str]:
    with _value_error_400():
        return kanban_db._normalize_board_slug(slug)


def _resolve_board(board: Optional[str]) -> Optional[str]:
    """Validate/normalise a board slug query param (400 malformed, 404 unknown);
    ``None`` when omitted so ``kb.connect()`` falls through to the active board."""
    if board is None or board == "":
        return None
    normed = _normalize_slug_or_400(board)
    if normed and normed != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {normed!r} does not exist")
    return normed


def _existing_board_slug(slug: str) -> str:
    """Normalise a path slug and require the board to exist (400 / 404)."""
    normed = _normalize_slug_or_400(slug)
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    return normed


def _conn(board: Optional[str] = None):
    """Connect to the already-normalised ``board`` (``None`` = active). ``init_db`` is
    idempotent; running it here lets a fresh install self-heal if POST /tasks arrives first."""
    import logging

    try:
        kanban_db.init_db(board=board)
    except Exception as exc:
        logging.getLogger(__name__).warning("kanban init_db failed: %s", exc)
    return kbc.connect(board=board)


@contextmanager
def _board_conn(board: Optional[str]) -> Iterator[tuple[Optional[str], sqlite3.Connection]]:
    """Resolve the ``board`` query param, open a connection, close it on exit."""
    board = _resolve_board(board)
    with closing(_conn(board=board)) as conn:
        yield board, conn


def _with_board_pinned(board: Optional[str], fn: Callable[[], Any]) -> Any:
    """Run ``fn`` with the board pinned context-locally, not via the process-global
    ``HERMES_KANBAN_BOARD`` env var (concurrent requests for different boards would cross-write)."""
    with kanban_db.scoped_current_board(_resolve_board(board) or kanban_db.DEFAULT_BOARD):
        return fn()


def _require(getter: Callable, conn: sqlite3.Connection, ident, label: str):
    obj = getter(conn, ident)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{label} {ident} not found")
    return obj


def _run_aux(board: Optional[str], module: str, fn: str, task_id: str, author: Optional[str]) -> Any:
    """Run a slow auxiliary-LLM task helper (``hermes_cli.<module>.<fn>``) with the board pinned;
    the module is imported lazily so a missing aux client can't break plugin load."""
    import importlib

    def _run():
        return getattr(importlib.import_module(f"hermes_cli.{module}"), fn)(task_id, author=(author or None))
    return _with_board_pinned(board, _run)


def _require_task(conn: sqlite3.Connection, task_id: str) -> kanban_db.Task:
    return _require(kanban_db.get_task, conn, task_id, "task")


def _require_run(conn: sqlite3.Connection, run_id: int) -> kanban_db.Run:
    return _require(kanban_db.get_run, conn, run_id, "run")


def _require_ok(ok: bool) -> None:
    """404 when a kanban_db mutator reports the task vanished mid-request."""
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


@contextmanager
def _map_errors(status: int, *types: type[BaseException]) -> Iterator[None]:
    """Map the given exception types to ``HTTPException(status, str(exc))``.

    A refusal carrying machine-readable fields (skill preflight) becomes a dict
    detail with the same ``code``/``profile``/``missing_skills`` contract the
    CLI and tool surfaces emit; everything else keeps its plain string detail.
    """
    try:
        yield
    except types as e:
        from hermes_cli.kanban_skill_preflight import structured_error_payload

        raise HTTPException(status_code=status, detail=structured_error_payload(e) or str(e))


_value_error_400 = partial(_map_errors, 400, ValueError)  # domain-layer validation refusals


@contextmanager
def _errors_to_500(prefix: str) -> Iterator[None]:
    """Map any unexpected exception to ``500 "<prefix>: <exc>"``; HTTPExceptions pass through."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{prefix}: {exc}")


def _attachment_dict(a: "kanban_db.Attachment") -> dict[str, Any]:
    """``stored_path`` is the absolute on-disk path workers read; UI downloads by ``id``."""
    return {
        "id": a.id, "task_id": a.task_id, "filename": a.filename, "content_type": a.content_type,
        "size": a.size, "uploaded_by": a.uploaded_by, "stored_path": a.stored_path, "created_at": a.created_at}


def _staged_attachment_dict(a: "kanban_db.StagedAttachment") -> dict[str, Any]:
    """Pre-submit paste flow; deliberately omits ``stored_path`` — the staged blob is a short-lived
    client-scoped intermediate the browser already has a local Blob/preview URL for."""
    return {"token": a.token, "filename": a.filename, "content_type": a.content_type, "size": a.size,
            "created_at": a.created_at}


def _projects_by_id() -> dict[str, Any]:
    """Map every project id -> Project (archived included) for annotation.

    Shared by the facade's ``GET /board/all`` (multi-board merge) and
    ``boards_router``'s board-listing endpoints, so both sides of the split
    annotate boards with the same project lookup.
    """
    try:
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as pconn:
            return {p.id: p for p in pdb.list_projects(pconn, include_archived=True)}
    except Exception:
        return {}


def _attachment_file_under_root(stored_path: str, board: Optional[str], label: str):
    """Defense in depth against a tampered DB row: the blob must still live under the board's
    attachments root and exist on disk (404 otherwise)."""
    from pathlib import Path

    root = kanban_db.attachments_root(board=board).resolve()
    try:
        stored = Path(stored_path).resolve()
        stored.relative_to(root)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail=f"{label} file unavailable")
    if not stored.is_file():
        raise HTTPException(status_code=404, detail=f"{label} file missing on disk")
    return stored
