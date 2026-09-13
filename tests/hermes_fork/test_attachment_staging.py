"""Chunked desktop file-attach staging tests (fork; moved from
tests/test_tui_gateway_server.py when the feature moved to
``hermes_fork.attachments.staging``).

The handlers are exercised through the real server dispatch, and the staging
state (``server._pending_uploads``) is asserted on the server module — the
bind_module seam publishes the fork module's helpers/state onto server.py's
namespace exactly like upstream split modules.
"""

import base64
import threading
import types

import tui_gateway.server as server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def test_file_attach_chunked_round_trip_materializes_full_file(tmp_path):
    """open -> two chunks -> commit reassembles the exact original bytes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    server._sessions["sid"] = _session(cwd=str(workspace), profile_home=str(home))

    try:
        opened = server.handle_request(
            {"id": "1", "method": "file.attach_open", "params": {"session_id": "sid"}}
        )
        upload_id = opened["result"]["upload_id"]
        assert upload_id

        part_a = b"hello "
        part_b = b"world"
        resp_a = server.handle_request(
            {
                "id": "2",
                "method": "file.attach_chunk",
                "params": {
                    "session_id": "sid",
                    "upload_id": upload_id,
                    "chunk_base64": base64.b64encode(part_a).decode("ascii"),
                },
            }
        )
        assert resp_a["result"]["bytes_written"] == len(part_a)

        resp_b = server.handle_request(
            {
                "id": "3",
                "method": "file.attach_chunk",
                "params": {
                    "session_id": "sid",
                    "upload_id": upload_id,
                    "chunk_base64": base64.b64encode(part_b).decode("ascii"),
                },
            }
        )
        assert resp_b["result"]["bytes_written"] == len(part_a) + len(part_b)

        commit = server.handle_request(
            {
                "id": "4",
                "method": "file.attach_commit",
                "params": {"session_id": "sid", "upload_id": upload_id, "name": "greeting.txt"},
            }
        )

        stored = home / "attachments" / "greeting.txt"
        assert commit["result"]["attached"] is True
        assert commit["result"]["uploaded"] is True
        assert commit["result"]["ref_text"] == f"@file:{stored}"
        assert stored.read_bytes() == part_a + part_b
        # No leftover temp file and no dangling pending-upload entry.
        assert upload_id not in server._pending_uploads
        assert not list((home / "attachments").glob(".upload-*.tmp"))
    finally:
        server._sessions.pop("sid", None)
        server._pending_uploads.clear()


def test_file_attach_chunk_rejects_unknown_upload_id():
    server._sessions["sid"] = _session()
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "file.attach_chunk",
                "params": {"session_id": "sid", "upload_id": "nope", "chunk_base64": "aGk="},
            }
        )
        assert "error" in resp
        assert resp["error"]["code"] == 4009
    finally:
        server._sessions.pop("sid", None)


def test_file_attach_chunk_rejects_cross_session_upload_id(tmp_path):
    """A different session's upload_id must not be appendable — scoped lookup."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server._sessions["owner"] = _session(cwd=str(workspace), profile_home=str(tmp_path / "home"))
    server._sessions["intruder"] = _session(cwd=str(workspace), profile_home=str(tmp_path / "home2"))

    try:
        opened = server.handle_request(
            {"id": "1", "method": "file.attach_open", "params": {"session_id": "owner"}}
        )
        upload_id = opened["result"]["upload_id"]

        resp = server.handle_request(
            {
                "id": "2",
                "method": "file.attach_chunk",
                "params": {"session_id": "intruder", "upload_id": upload_id, "chunk_base64": "aGk="},
            }
        )
        assert "error" in resp
        assert resp["error"]["code"] == 4009

        commit = server.handle_request(
            {
                "id": "3",
                "method": "file.attach_commit",
                "params": {"session_id": "intruder", "upload_id": upload_id, "name": "x.txt"},
            }
        )
        assert "error" in commit
        assert commit["error"]["code"] == 4009
    finally:
        server._sessions.pop("owner", None)
        server._sessions.pop("intruder", None)
        server._pending_uploads.clear()


def test_file_attach_chunk_enforces_upload_cap(monkeypatch, tmp_path):
    """A chunk pushing the running total over the cap is rejected, not truncated-committed."""
    monkeypatch.setattr(server, "_FILE_ATTACH_UPLOAD_MAX_BYTES", 4)
    server._sessions["sid"] = _session(cwd=str(tmp_path), profile_home=str(tmp_path / "home"))

    try:
        opened = server.handle_request(
            {"id": "1", "method": "file.attach_open", "params": {"session_id": "sid"}}
        )
        upload_id = opened["result"]["upload_id"]

        resp = server.handle_request(
            {
                "id": "2",
                "method": "file.attach_chunk",
                "params": {
                    "session_id": "sid",
                    "upload_id": upload_id,
                    "chunk_base64": base64.b64encode(b"toolong").decode("ascii"),
                },
            }
        )

        assert "error" in resp
        assert "too large" in resp["error"]["message"]
    finally:
        server._sessions.pop("sid", None)
        server._abort_pending_upload(upload_id, "sid")
        server._pending_uploads.clear()


def test_file_attach_abort_discards_temp_file(tmp_path):
    home = tmp_path / "home"
    server._sessions["sid"] = _session(cwd=str(tmp_path), profile_home=str(home))

    try:
        opened = server.handle_request(
            {"id": "1", "method": "file.attach_open", "params": {"session_id": "sid"}}
        )
        upload_id = opened["result"]["upload_id"]
        assert list((home / "attachments").glob(".upload-*.tmp"))

        resp = server.handle_request(
            {
                "id": "2",
                "method": "file.attach_abort",
                "params": {"session_id": "sid", "upload_id": upload_id},
            }
        )
        assert resp["result"]["aborted"] is True
        assert not list((home / "attachments").glob(".upload-*.tmp"))
        assert upload_id not in server._pending_uploads

        # Aborting again (or committing) a gone upload is a clean miss, not a crash.
        resp2 = server.handle_request(
            {
                "id": "3",
                "method": "file.attach_abort",
                "params": {"session_id": "sid", "upload_id": upload_id},
            }
        )
        assert resp2["result"]["aborted"] is False
    finally:
        server._sessions.pop("sid", None)
        server._pending_uploads.clear()
