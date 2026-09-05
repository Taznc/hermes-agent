"""Attachment staging: image sniffing, size caps, per-session attachment dirs, path resolution.

Bodies are rebound onto server.py's globals at install time (see
method_ctx.bind_module), so they reference server.py globals bare.
"""

from __future__ import annotations

import re as _re

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()


_ATTACH_BYTES_MAX_BYTES = 25 * 1024 * 1024
_PDF_ATTACH_MAX_BYTES = 50 * 1024 * 1024
_PDF_ATTACH_MAX_PAGES = 25

# Leading magic bytes -> file extension, for filename-less uploads.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"), (b"BM", ".bmp"))

# Context-ref values containing any of these must be quoted (desktop formatRefValue parity).
_ATTACHMENT_REF_NEEDS_QUOTING_RE = _re.compile(r"""[\s()\[\]{}<>"'`]""")
del _re  # bodies are rebound onto server globals: import inside functions only


def _b64_payload(raw: str, data_url_re: str, flags: int) -> bytes:
    """Strip an optional ``data:...;base64,`` wrapper and all whitespace, then strictly decode."""
    import base64 as _base64
    import re as _re
    cleaned = (raw or "").strip()
    if m := _re.match(data_url_re, cleaned, flags):
        cleaned = m.group(1)
    return _base64.b64decode(_re.sub(r"\s+", "", cleaned), validate=True)


def _decode_attach_base64(raw: str, *, mime_prefix: str) -> bytes | None:
    """Decode a (``data:<mime_prefix>...;base64,``-wrapped) payload; None when invalid."""
    import re as _re
    try:
        return _b64_payload(
            raw, rf"^data:{_re.escape(mime_prefix)}[a-zA-Z0-9.+-]*;base64,(.*)$", _re.DOTALL)
    except Exception:
        return None


def _decode_attach_payload(
    rid, raw_b64: str, *, mime_prefix: str, max_bytes: int, label: str, empty_msg: str):
    """``(bytes, None)`` or ``(None, error)``: 4017 on bad/empty base64, 4018 over *max_bytes*."""
    data = _decode_attach_base64(raw_b64, mime_prefix=mime_prefix)
    if data is None:
        return None, _err(rid, 4017, "data is not valid base64")
    if not data:
        return None, _err(rid, 4017, empty_msg)
    if len(data) > max_bytes:
        mb = max_bytes // (1024 * 1024)
        return None, _err(rid, 4018, f"{label} too large ({len(data)} bytes; cap is {mb} MB)")
    return data, None


def _sniff_image_ext(img_bytes: bytes, filename: str = "") -> str:
    """Extension from the filename hint, else magic bytes (WebP: RIFF container), else ``.png``."""
    if filename and (suffix := Path(filename).suffix.lower()):
        return suffix
    head = img_bytes[:16]
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    return next((ext for sig, ext in _IMAGE_MAGIC if head.startswith(sig)), ".png")


def _allowed_image_extensions() -> frozenset[str]:
    try:
        from cli import _IMAGE_EXTENSIONS
        return frozenset(_IMAGE_EXTENSIONS)
    except Exception:
        return frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def _session_home_dir(session: dict, name: str) -> Path:
    """``<session home>/<name>``, anchored on the session's stored ``profile_home``: attach
    RPCs run BEFORE ``prompt.submit`` installs the profile HERMES_HOME override, while
    the sandbox mounts and the vision host-read allowlist resolve the *session profile's*
    dirs at run time — writing anywhere else means the agent can never see the file."""
    profile_home = session.get("profile_home")
    return (Path(profile_home) if profile_home else _hermes_home) / name


def _session_images_dir(session: dict) -> Path:
    return _session_home_dir(session, "images")


def _queue_attached_image(session: dict, img_bytes: bytes, ext: str, *, prefix: str) -> Path:
    """Write image bytes into the session images dir and queue them for the next submit."""
    session["image_counter"] = session.get("image_counter", 0) + 1
    img_dir = _session_images_dir(session)
    img_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_path = img_dir / f"{prefix}_{ts}_{session['image_counter']}{ext}"
    try:
        img_path.write_bytes(img_bytes)
    except Exception:
        session["image_counter"] = max(0, session["image_counter"] - 1)
        raise
    session.setdefault("attached_images", []).append(str(img_path))
    return img_path


def _format_ref_value(value: str) -> str:
    """Quote a value with whitespace/brackets/quotes so the ``@file:`` ref round-trips."""
    if not value or not _ATTACHMENT_REF_NEEDS_QUOTING_RE.search(value):
        return value
    for q in ("`", '"', "'"):
        if q not in value:
            return f"{q}{value}{q}"
    return value


def _attachment_ref_path(session: dict, target: Path) -> str:
    """Workspace-relative path for an attachment, or the absolute path if outside."""
    workspace = Path(_session_cwd(session)).resolve()
    try:
        return str(target.resolve().relative_to(workspace)).replace(os.sep, "/")
    except ValueError:
        return str(target.resolve())


def _sanitize_attachment_name(name: str) -> str:
    import re as _re
    candidate = _re.sub(r"[\x00-\x1f]+", "_", Path(str(name or "").strip()).name)
    return candidate.strip().strip(".") or "attachment"


def _stage_session_file_attachment(
    session: dict, *, raw_path: str, data_url: str, name: str) -> tuple[Path, bool]:
    """Make a desktop file attachment available to the gateway agent: ``(stored_path, uploaded)``.
    Inside the workspace -> as-is; gateway-visible but outside -> copied into ``attachments/``
    (bind-mounted into container backends so ``@file:`` resolves in the sandbox); not on the
    gateway -> ``data_url`` bytes decoded into ``attachments/``."""
    workspace = Path(_session_cwd(session)).resolve()
    resolved = None
    if raw_path:
        try:
            from cli import _detect_file_drop, _resolve_attachment_path, _split_path_input
        except Exception:
            _detect_file_drop = None
        if _detect_file_drop is not None:
            dropped = _detect_file_drop(raw_path)
            if dropped:
                resolved = Path(dropped["path"]).resolve()
            else:
                path_token, _remainder = _split_path_input(raw_path)
                found = _resolve_attachment_path(path_token)
                resolved = Path(found).resolve() if found is not None else None
    if resolved is not None:
        try:
            resolved.relative_to(workspace)
            return resolved, False
        except ValueError:
            payload = resolved.read_bytes()
            filename = resolved.name
    else:
        if not data_url:
            raise ValueError("file not found on gateway and no data_url provided")
        # Any media type (unlike the image-specific decoder); bare base64 also accepted.
        import binascii as _binascii
        import re as _re
        try:
            payload = _b64_payload(
                data_url, r"^data:[^;,]*(?:;[^;,=]+=[^;,]+)*;base64,(.*)$", _re.DOTALL | _re.I)
        except (ValueError, _binascii.Error) as exc:
            raise ValueError("invalid data_url payload") from exc
        filename = _sanitize_attachment_name(name or Path(str(raw_path or "")).name)
    root = _session_home_dir(session, "attachments")
    root.mkdir(parents=True, exist_ok=True)
    filename = _sanitize_attachment_name(filename)
    target = root / filename
    if target.exists():
        stem = Path(filename).stem or "attachment"
        suffix = Path(filename).suffix
        counter = 2
        while (target := root / f"{stem}-{counter}{suffix}").exists():
            counter += 1
    target.write_bytes(payload)
    return target.resolve(), True


# ── Chunked desktop file-attach staging (streams disk → gateway in bounded slices instead of one
# whole-file base64 JSON-RPC frame; t_275f8015) ──────────────────────────────────────────────────
#
# Mirrors ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES in apps/desktop/electron/hardening.ts so the two ends
# of the transport agree on the cap without importing across the language boundary.
_FILE_ATTACH_UPLOAD_MAX_BYTES = 256 * 1024 * 1024
# Abandoned uploads (renderer crash, network drop before attach_commit/_abort) must not leak temp
# files or _pending_uploads entries forever. Swept opportunistically from file.attach_open rather
# than on a background timer — uploads are short-lived, so "next open pays the sweep" never leaves
# an unbounded backlog between opens.
_PENDING_UPLOAD_STALE_SECONDS = 3600.0


def _desktop_attachment_dir(session: dict) -> Path:
    """The session's ``attachments/`` dir (created), where chunked uploads are staged and land."""
    root = _session_home_dir(session, "attachments")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _unique_attachment_path(root: Path, filename: str) -> Path:
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
    session: dict, session_key: str, upload_id: str, *, name: str, raw_path: str) -> Path:
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


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
