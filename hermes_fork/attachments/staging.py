"""Chunked desktop file-attach staging (fork; streams disk → gateway in bounded
slices instead of one whole-file base64 JSON-RPC frame; t_275f8015).

Extracted from ``tui_gateway/prompt_attachments.py`` + ``tui_gateway/methods_prompt.py``
behind the ``# >>> FORK ANCHOR: gateway-fork-methods <<<`` seam in
``tui_gateway/server.py``. Bodies are rebound onto server.py's globals at
install time (see ``tui_gateway.method_ctx.bind_module``), so they reference
server.py globals bare — session authorization (``_sess_building``), attachment
naming/refs (``_sanitize_attachment_name``/``_attachment_ref_path``/
``_format_ref_value``) stay upstream-owned dependencies.
"""

from __future__ import annotations

import threading

from tui_gateway.method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


# Mirrors ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES in apps/desktop/electron/hardening.ts so the two ends
# of the transport agree on the cap without importing across the language boundary.
_FILE_ATTACH_UPLOAD_MAX_BYTES = 256 * 1024 * 1024
# Abandoned uploads (renderer crash, network drop before attach_commit/_abort) must not leak temp
# files or _pending_uploads entries forever. Swept opportunistically from file.attach_open rather
# than on a background timer — uploads are short-lived, so "next open pays the sweep" never leaves
# an unbounded backlog between opens.
_PENDING_UPLOAD_STALE_SECONDS = 3600.0

# Each entry is one in-progress upload's temp-file path + bookkeeping, keyed by a random upload_id.
# The lock guards two racing chunk appends for the same upload; no OS handle stays open across RPC
# round-trips (each chunk append opens/writes/closes), so an abandoned upload leaks only its temp
# file, which _reap_stale_pending_uploads sweeps.
_pending_uploads_lock = threading.Lock()
_pending_uploads: dict[str, dict] = {}


def _desktop_attachment_dir(session: dict) -> "Path":
    """The session's ``attachments/`` dir (created), where chunked uploads are staged and land."""
    root = _session_home_dir(session, "attachments")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _unique_attachment_path(root: "Path", filename: str) -> "Path":
    """``root/filename``, suffixing ``-2``, ``-3``, … while the name is taken."""
    target = root / filename
    if target.exists():
        stem = Path(filename).stem or "attachment"
        suffix = Path(filename).suffix
        counter = 2
        while (target := root / f"{stem}-{counter}{suffix}").exists():
            counter += 1
    return target


def _reap_stale_pending_uploads() -> None:
    """Delete tmp files + entries for uploads abandoned over an hour ago."""
    now = time.time()
    with _pending_uploads_lock:
        stale_ids = [
            upload_id for upload_id, entry in _pending_uploads.items()
            if now - entry.get("created_at", now) > _PENDING_UPLOAD_STALE_SECONDS]
        stale_entries = [_pending_uploads.pop(upload_id) for upload_id in stale_ids]
    for entry in stale_entries:
        try:
            Path(entry["tmp_path"]).unlink(missing_ok=True)
        except Exception:
            logger.debug("failed to reap stale pending upload tmp file", exc_info=True)


def _open_pending_upload(session: dict, session_key: str) -> str:
    """Create a fresh temp file for a chunked upload and register it. Returns the ``upload_id``.
    The temp file lives in the SAME directory chunk data ultimately lands in (the session's
    ``attachments/``), so the final commit is a same-filesystem rename rather than a copy."""
    _reap_stale_pending_uploads()
    upload_dir = _desktop_attachment_dir(session)
    upload_id = uuid.uuid4().hex
    tmp_path = upload_dir / f".upload-{upload_id}.tmp"
    tmp_path.touch(exist_ok=False)
    with _pending_uploads_lock:
        _pending_uploads[upload_id] = {
            "tmp_path": str(tmp_path), "session_key": session_key, "bytes_written": 0,
            "created_at": time.time()}
    return upload_id


def _pending_upload_for(upload_id: str, session_key: str) -> dict | None:
    """Pending upload scoped to the session that opened it; cross-session access returns ``None``
    (surfaced as 4009 by callers) so one session can never touch another's in-flight upload."""
    with _pending_uploads_lock:
        entry = _pending_uploads.get(upload_id)
    if entry is None or entry.get("session_key") != session_key:
        return None
    return entry


def _append_pending_upload_chunk(upload_id: str, session_key: str, chunk: bytes) -> int:
    """Append decoded bytes to the upload's temp file; returns total bytes so far. Raises
    ``ValueError`` on an unknown/foreign upload_id or when the cap would be exceeded."""
    entry = _pending_upload_for(upload_id, session_key)
    if entry is None:
        raise ValueError("unknown upload_id")
    projected = entry["bytes_written"] + len(chunk)
    if projected > _FILE_ATTACH_UPLOAD_MAX_BYTES:
        raise ValueError(
            f"file is too large ({projected} bytes; limit {_FILE_ATTACH_UPLOAD_MAX_BYTES} bytes)")
    with open(entry["tmp_path"], "ab") as handle:
        handle.write(chunk)
    with _pending_uploads_lock:
        live = _pending_uploads.get(upload_id)
        if live is not None:
            live["bytes_written"] = projected
    return projected


def _commit_pending_upload(
    session: dict, session_key: str, upload_id: str, *, name: str, raw_path: str) -> "Path":
    """Finalize a chunked upload: rename its temp file into place. Removes the bookkeeping either
    way so a failed commit can't be retried into a double-materialized file."""
    with _pending_uploads_lock:
        entry = _pending_uploads.pop(upload_id, None)
    if entry is None or entry.get("session_key") != session_key:
        raise ValueError("unknown upload_id")
    tmp_path = Path(entry["tmp_path"])
    try:
        filename = _sanitize_attachment_name(name or Path(str(raw_path or "")).name)
        target = _unique_attachment_path(_desktop_attachment_dir(session), filename)
        tmp_path.replace(target)
        return target.resolve()
    finally:
        # replace() already moved it on success (no-op unlink); on failure the tmp file is orphaned
        # bytes with no retry path (the entry is gone), so clean it up rather than leak it.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _abort_pending_upload(upload_id: str, session_key: str) -> bool:
    """Discard an in-progress upload's temp file. Returns whether one existed."""
    with _pending_uploads_lock:
        entry = _pending_uploads.pop(upload_id, None)
    if entry is None or entry.get("session_key") != session_key:
        return False
    try:
        Path(entry["tmp_path"]).unlink(missing_ok=True)
    except Exception:
        logger.debug("failed to remove aborted upload tmp file", exc_info=True)
    return True


@method("file.attach_open")
def _(rid, params: dict) -> dict:
    """Begin a chunked non-image file upload. Returns an ``upload_id``.

    Streaming counterpart to ``file.attach``: instead of one JSON-RPC frame
    carrying the whole file as base64 (main-process freeze + ~3x memory on
    the desktop side for large attachments — see t_275f8015), the client opens
    an upload, POSTs bounded ``file.attach_chunk`` calls, then finalizes with
    ``file.attach_commit``. At no point does the gateway hold more than one
    chunk's decoded bytes in memory; chunks are appended straight to a temp
    file under the session's ``attachments/`` dir.

    Params:
      session_id (str, required)
    """
    session, err = _sess_building(params, rid)
    if err:
        return err
    session_id = str(params.get("session_id", "") or "").strip()
    try:
        upload_id = _open_pending_upload(session, session_id)
        return _ok(rid, {"upload_id": upload_id})
    except Exception as e:
        return _err(rid, 5028, str(e))


@method("file.attach_chunk")
def _(rid, params: dict) -> dict:
    """Append one base64-encoded slice to an open chunked upload.

    Params:
      session_id (str, required)
      upload_id (str, required): from ``file.attach_open``.
      chunk_base64 (str, required): raw base64 (no ``data:`` prefix) for this
        slice. The caller is responsible for ordering slices correctly —
        chunks are appended in the order received, not reordered by offset.

    Returns ``{"bytes_written": <total bytes appended so far>}``.
    """
    session, err = _sess_building(params, rid)
    if err:
        return err
    session_id = str(params.get("session_id", "") or "").strip()
    upload_id = str(params.get("upload_id", "") or "").strip()
    chunk_b64 = str(params.get("chunk_base64", "") or "")
    if not upload_id:
        return _err(rid, 4015, "upload_id required")
    try:
        import base64 as _base64
        import binascii as _binascii

        try:
            chunk = _base64.b64decode(chunk_b64, validate=True) if chunk_b64 else b""
        except (ValueError, _binascii.Error) as exc:
            return _err(rid, 4017, f"invalid chunk_base64: {exc}")
        bytes_written = _append_pending_upload_chunk(upload_id, session_id, chunk)
        return _ok(rid, {"bytes_written": bytes_written})
    except ValueError as e:
        return _err(rid, 4009, str(e))
    except Exception as e:
        return _err(rid, 5028, str(e))


@method("file.attach_commit")
def _(rid, params: dict) -> dict:
    """Finalize a chunked upload into a session attachment (mirrors file.attach's result shape).

    Params:
      session_id (str, required)
      upload_id (str, required): from ``file.attach_open``.
      path (str, optional): client/host path (used for naming).
      name (str, optional): preferred filename.
    """
    session, err = _sess_building(params, rid)
    if err:
        return err
    session_id = str(params.get("session_id", "") or "").strip()
    upload_id = str(params.get("upload_id", "") or "").strip()
    raw = str(params.get("path", "") or "").strip()
    name = str(params.get("name", "") or "").strip()
    if not upload_id:
        return _err(rid, 4015, "upload_id required")
    try:
        stored_path = _commit_pending_upload(
            session, session_id, upload_id, name=name, raw_path=raw
        )
        ref_path = _attachment_ref_path(session, stored_path)
        return _ok(
            rid,
            {
                "attached": True,
                "name": stored_path.name,
                "path": str(stored_path),
                "ref_path": ref_path,
                "ref_text": f"@file:{_format_ref_value(ref_path)}",
                "uploaded": True,
            },
        )
    except ValueError as e:
        return _err(rid, 4009, str(e))
    except Exception as e:
        return _err(rid, 5028, str(e))


@method("file.attach_abort")
def _(rid, params: dict) -> dict:
    """Discard an in-progress chunked upload (renderer cancel/error path).

    Params:
      session_id (str, required)
      upload_id (str, required): from ``file.attach_open``.
    """
    session, err = _sess_building(params, rid)
    if err:
        return err
    session_id = str(params.get("session_id", "") or "").strip()
    upload_id = str(params.get("upload_id", "") or "").strip()
    if not upload_id:
        return _err(rid, 4015, "upload_id required")
    aborted = _abort_pending_upload(upload_id, session_id)
    return _ok(rid, {"aborted": aborted})


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
