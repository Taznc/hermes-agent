"""Kanban dashboard — boards CRUD (multi-project support): list/create/rename/delete/export/import/switch."""

from __future__ import annotations

import asyncio
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

from plugins.kanban.dashboard._common import (
    _errors_to_500,
    _existing_board_slug,
    _projects_by_id,
    _value_error_400,
)

router = APIRouter()


class CreateBoardBody(BaseModel):
    slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    default_workdir: Optional[str] = None
    # Project (id or slug) scoping the board: default_workdir mirrors its primary repo, tasks inherit it.
    project_id: Optional[str] = None
    switch: bool = False


class RenameBoardBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    # For both fields: ``None`` = leave unchanged; "" = clear; value = validate/resolve + set.
    default_workdir: Optional[str] = None
    project_id: Optional[str] = None


# Board transfer exchanges filesystem PATHS, not bytes (same contract as profile export/import):
# clients run the native save/open dialog on the machine hosting the backend.

class ExportBoardBody(BaseModel):
    output: str = ""  # empty → staging path under the kanban root
    attachments: bool = True
    logs: bool = False


class ImportBoardBody(BaseModel):
    archive: str  # path to a board .tar.gz on the backend's filesystem
    slug: Optional[str] = None  # override the archive's slug; collisions auto-suffix
    switch: bool = False


def _board_display_kwargs(p: BaseModel) -> dict[str, Any]:
    """Display-metadata fields shared by create_board / write_board_metadata."""
    return {"name": p.name, "description": p.description, "icon": p.icon, "color": p.color}


def _resolve_project(ref: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve a project id/slug to ``(id, name, primary_path)``; ``(None,)*3``
    for a falsy ref, 400 when a non-empty ref doesn't resolve."""
    if not ref or not ref.strip():
        return None, None, None
    with _errors_to_500("projects unavailable"):
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as pconn:
            proj = pdb.get_project(pconn, ref.strip())
    if proj is None:
        raise HTTPException(status_code=400, detail=f"project {ref!r} does not exist")
    return proj.id, proj.name, (proj.primary_path or None)


def _board_counts(slug: str) -> dict[str, int]:
    """``{status: count}`` for a board; ``{}`` on a missing/empty DB."""
    try:
        if not kanban_db.kanban_db_path(board=slug).exists():
            return {}
        with closing(kbc.connect(board=slug)) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _default_workspace_kind(board: dict[str, Any]) -> str:
    """Recommend a non-destructive task workspace from board metadata."""
    workdir = str(board.get("default_workdir") or "").strip()
    if not workdir:
        return "scratch"
    try:
        return "worktree" if kbw._git_toplevel(Path(workdir)) else "dir"
    except (OSError, ValueError):
        return "dir"


def _annotate_board_meta(meta: dict) -> dict:
    meta["default_workspace_kind"] = _default_workspace_kind(meta)
    _, meta["project_name"], _ = _resolve_project(meta.get("project_id"))
    return meta


@router.get("/projects")
def list_kanban_projects():
    """Live (non-archived) projects available for board scoping."""
    with _errors_to_500("failed to list projects"):
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as pconn:
            projects = pdb.list_projects(pconn, include_archived=False)
    return {"projects": [
        {"id": p.id, "slug": p.slug, "name": p.name,
         "primary_path": p.primary_path or "", "icon": p.icon or "", "color": p.color or ""}
        for p in projects]}


@router.get("/boards")
def list_boards(include_archived: bool = Query(False)):
    """Every board on disk with task counts and the active slug."""
    boards = kanban_db.list_boards(include_archived=include_archived)
    current = kanban_db.get_current_board()
    proj_map = _projects_by_id()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_counts(b["slug"])
        # Live cards only — archived tasks are hidden from every default board view,
        # so counting them in the switcher badge would visibly disagree.
        b["total"] = sum(n for status, n in b["counts"].items() if status != "archived")
        b["default_workspace_kind"] = _default_workspace_kind(b)
        pid = b["project_id"] = b.get("project_id") or None
        proj = proj_map.get(pid) if pid else None
        b["project_name"] = proj.name if proj else None
    return {"boards": boards, "current": current}


def _validate_workdir(raw: str) -> str:
    """Board default_workdir must be an absolute, existing directory (400 otherwise)."""
    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        raise HTTPException(status_code=400, detail="Project directory must be an absolute path.")
    if not requested.is_dir():
        raise HTTPException(status_code=400, detail="Project directory must be an existing directory.")
    return str(requested.resolve())


@router.post("/boards")
def create_board_endpoint(payload: CreateBoardBody):
    """Create a board. Idempotent — ``slug`` collision returns the existing one."""
    default_workdir = _validate_workdir(payload.default_workdir) if payload.default_workdir else None
    # A chosen project's primary repo becomes the default workdir unless one was passed explicitly.
    project_id, _pname, primary_path = _resolve_project(payload.project_id)
    if primary_path and not default_workdir:
        default_workdir = primary_path
    with _value_error_400():
        meta = kanban_db.create_board(
            payload.slug, default_workdir=default_workdir, project_id=project_id, **_board_display_kwargs(payload))
    if payload.switch:
        with _value_error_400():
            kanban_db.set_current_board(meta["slug"])
    return {"board": _annotate_board_meta(meta), "current": kanban_db.get_current_board()}


@router.patch("/boards/{slug}")
def rename_board(slug: str, payload: RenameBoardBody):
    """Update display metadata / default workdir / project scope (slug is immutable)."""
    normed = _existing_board_slug(slug)
    # write_board_metadata treats a falsy value as "clear", so pass "" through.
    default_workdir: Optional[str] = None
    if payload.default_workdir is not None:
        raw = payload.default_workdir.strip()
        default_workdir = _validate_workdir(raw) if raw else ""
    # A resolved project mirrors its repo into default_workdir unless the caller set it explicitly.
    project_id: Optional[str] = None
    if payload.project_id is not None:
        if payload.project_id.strip():
            project_id, _pname, primary_path = _resolve_project(payload.project_id)
            if primary_path and default_workdir is None:
                default_workdir = primary_path
        else:
            project_id = ""  # clear the scope
    meta = kanban_db.write_board_metadata(
        normed, default_workdir=default_workdir, project_id=project_id, **_board_display_kwargs(payload))
    return {"board": _annotate_board_meta(meta)}


@router.delete("/boards/{slug}")
def delete_board(slug: str, delete: bool = Query(False, description="Hard-delete instead of archive")):
    """Archive (default) or hard-delete a board."""
    with _value_error_400():
        res = kanban_db.remove_board(slug, archive=not delete)
    return {"result": res, "current": kanban_db.get_current_board()}


async def _run_transfer(fn, log_label: str):
    """Run a blocking kanban_transfer call off the event loop, mapping its errors
    to 404 (missing path) / 400 (invalid) / 500 (logged)."""
    import logging

    try:
        return await asyncio.get_running_loop().run_in_executor(None, fn)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.getLogger(__name__).exception("%s failed", log_label)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/boards/{slug}/export")
async def export_board_endpoint(slug: str, body: ExportBoardBody):
    """Write ``slug`` to a portable archive; return the path written."""
    from hermes_cli import kanban_transfer

    output = (body.output or "").strip()
    if not output:
        staging = kanban_db.kanban_home() / "kanban" / "board-exports"
        try:
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not create export directory: {exc}")
        output = str(staging / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    return await _run_transfer(
        lambda: kanban_transfer.export_board(slug, output, include_attachments=body.attachments, include_logs=body.logs),
        f"POST /boards/{slug}/export")


@router.post("/boards/import")
async def import_board_endpoint(body: ImportBoardBody):
    """Import a board archive as a NEW board; return the landed board."""
    from hermes_cli import kanban_transfer

    archive = (body.archive or "").strip()
    if not archive:
        raise HTTPException(status_code=400, detail="archive path is required")
    result = await _run_transfer(
        lambda: kanban_transfer.import_board(archive, (body.slug or "").strip() or None, activate=body.switch),
        "POST /boards/import")
    return {**result, "current": kanban_db.get_current_board()}


@router.post("/boards/{slug}/switch")
def switch_board(slug: str):
    """Persist ``slug`` as the active board for CLI / slash-command parity
    (dashboard users pick boards client-side via localStorage)."""
    normed = _existing_board_slug(slug)
    kanban_db.set_current_board(normed)
    return {"current": normed}
