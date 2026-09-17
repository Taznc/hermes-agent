"""Kanban dashboard — attachments + staged attachments routes.

Size cap, filename sanitiser, and collision resolver live in ``kanban_db`` so the
dashboard, agent toolset, and CLI share one implementation. Staged attachments
(pre-task-creation pasted images) are distinct from task attachments because no
task_id exists yet: the "new task" dialog accepts & previews a pasted image
before Create (docs/design/kanban-task-image-attachments.md). Image-only (mime
allowlist + 10 MB cap), unlike the generic (any mime, 25 MB) task upload.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status as http_status
from fastapi.responses import FileResponse

from hermes_cli import kanban_db
from hermes_cli.kanban_db import KANBAN_ATTACHMENT_MAX_BYTES, _collision_free_path, _safe_attachment_name

from plugins.kanban.dashboard._common import (
    _attachment_dict,
    _attachment_file_under_root,
    _board_conn,
    _require_task,
    _resolve_board,
    _staged_attachment_dict,
    _value_error_400,
)

router = APIRouter()


@router.get("/tasks/{task_id}/attachments")
def list_task_attachments(task_id: str, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        return {"attachments": [_attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)]}


@router.post("/tasks/{task_id}/attachments")
async def upload_task_attachment(
    task_id: str,
    file: UploadFile = File(...),
    board: Optional[str] = Query(None),
    uploaded_by: Optional[str] = Form(None)):
    """Store an upload under ``attachments_root(board)/<task_id>/`` (sanitised,
    collision-resolved name; ``_safe_attachment_name`` ValueError → 400) and record it."""
    with _board_conn(board) as (board, conn), _value_error_400():
        _require_task(conn, task_id)
        safe_name = _safe_attachment_name(file.filename or "")
        dest_dir = kanban_db.task_attachments_dir(task_id, board=board)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = _collision_free_path(dest_dir, safe_name)  # foo.pdf → foo (1).pdf …
        total = 0  # stream in chunks with a hard size cap so one upload can't fill the disk
        try:
            with open(dest_path, "wb") as out:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > KANBAN_ATTACHMENT_MAX_BYTES:
                        out.close()
                        dest_path.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413, detail=f"attachment exceeds {KANBAN_ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB limit")
                    out.write(chunk)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"failed to store attachment: {exc}")
        att_id = kanban_db.add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=file.content_type, size=total, uploaded_by=(uploaded_by or "dashboard"))
        att = kanban_db.get_attachment(conn, att_id)
        return {"attachment": _attachment_dict(att) if att else None}


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        att = kanban_db.get_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        # Defense in depth against a tampered DB row: the blob must still live under the board's attachments root.
        root = kanban_db.attachments_root(board=board).resolve()
        try:
            stored = Path(att.stored_path).resolve()
            stored.relative_to(root)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="attachment file unavailable")
        if not stored.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing on disk")
        return FileResponse(path=str(stored), filename=att.filename, media_type=att.content_type or "application/octet-stream")


@router.delete("/attachments/{attachment_id}")
def remove_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        if kanban_db.delete_attachment(conn, attachment_id) is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        return {"ok": True, "id": attachment_id}


# Inline-rendering cap for the data-url endpoint: Desktop plugin REST goes through the Electron IPC
# bridge (JSON/ArrayBuffer only, no streamed byte range a plain <img> could point at). A 10 MB pasted
# image (KANBAN_IMAGE_ATTACHMENT_MAX_BYTES) base64-inlines fine; this guards against a larger GENERIC
# attachment (25 MB cap) that was never meant for inline rendering — those stay download-only.
_ATTACHMENT_INLINE_MAX_BYTES = 12 * 1024 * 1024


@router.get("/attachments/{attachment_id}/data-url")
def attachment_data_url(attachment_id: int, board: Optional[str] = Query(None)):
    """Attachment bytes as a base64 data URL (JSON body) — the desktop plugin host has no
    authenticated binary-fetch door, so rendering a pasted image inline needs the bytes delivered
    as a data URL rather than a URL to point an ``<img>`` at."""
    with _board_conn(board) as (board, conn):
        att = kanban_db.get_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        stored = _attachment_file_under_root(att.stored_path, board, "attachment")
        size = stored.stat().st_size
        if size > _ATTACHMENT_INLINE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"attachment exceeds {_ATTACHMENT_INLINE_MAX_BYTES // (1024 * 1024)} MB inline-render limit; download it instead")
        mime = att.content_type or "application/octet-stream"
        encoded = base64.b64encode(stored.read_bytes()).decode("ascii")
        return {"data_url": f"data:{mime};base64,{encoded}", "content_type": mime, "size": size}


@router.post("/attachments/staged", status_code=http_status.HTTP_201_CREATED)
async def upload_staged_attachment(
    file: UploadFile = File(...), board: Optional[str] = Query(None), uploaded_by: Optional[str] = Form(None)):
    """Stage a pasted image before its owning task exists: mime allowlist up front (400), then a
    streamed read with a hard cap (413), matching ``upload_task_attachment``."""
    board = _resolve_board(board)
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in kanban_db.KANBAN_IMAGE_ALLOWED_MIME_TYPES:
        accepted = ", ".join(sorted(kanban_db.KANBAN_IMAGE_ALLOWED_MIME_TYPES))
        raise HTTPException(
            status_code=400, detail=f"unsupported image type: {file.content_type or 'unknown'}; accepted: {accepted}")
    with _value_error_400():
        safe_name = _safe_attachment_name(file.filename or "")
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > kanban_db.KANBAN_IMAGE_ATTACHMENT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"attachment exceeds {kanban_db.KANBAN_IMAGE_ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB limit")
        chunks.append(chunk)
    try:
        staged = kanban_db.stage_attachment_bytes(
            safe_name, b"".join(chunks), content_type=file.content_type,
            uploaded_by=(uploaded_by or "dashboard"), board=board)
    except kanban_db.AttachmentTooLarge as e:
        raise HTTPException(status_code=413, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"attachment": _staged_attachment_dict(staged)}


@router.get("/attachments/staged/{token}")
def download_staged_attachment(token: str, board: Optional[str] = Query(None)):
    """Serve a staged blob (dialog reload / multi-tab parity with ``GET /attachments/{id}``)."""
    board = _resolve_board(board)
    staged = kanban_db.get_staged_attachment(token, board=board)
    if staged is None:
        raise HTTPException(status_code=404, detail="staged attachment not found")
    stored = _attachment_file_under_root(staged.stored_path, board, "staged attachment")
    return FileResponse(path=str(stored), filename=staged.filename,
                        media_type=staged.content_type or "application/octet-stream")


@router.delete("/attachments/staged/{token}")
def remove_staged_attachment(token: str, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    if not kanban_db.delete_staged_attachment(token, board=board):
        raise HTTPException(status_code=404, detail="staged attachment not found")
    return {"ok": True, "token": token}
