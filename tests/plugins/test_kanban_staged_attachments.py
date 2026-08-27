"""Tests for pre-task-creation pasted-image "staged" attachments.

See docs/design/kanban-task-image-attachments.md. Covers:
  * ``hermes_cli.kanban_db`` staging accessors (stage/get/delete/promote/reap)
  * the dashboard REST surface (``POST/GET/DELETE /attachments/staged*``)
  * the ``POST /tasks`` promotion path (``pending_attachment_tokens``)
  * that existing task creation/update flows are unaffected by the new field

Follows the same approach as ``test_kanban_attachments.py``: the plugin
router is attached to a bare FastAPI app so the real HTTP path (multipart
upload, streaming download) is exercised without the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# A minimal valid 1x1 PNG (transparent pixel) — real magic bytes, not just a
# fake content-type label, in case any future validation sniffs the body.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000155f6c8f60000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_kanban_attachments.py)
# ---------------------------------------------------------------------------


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_staged_attach_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


# ---------------------------------------------------------------------------
# DB-layer accessors
# ---------------------------------------------------------------------------


def test_stage_attachment_bytes_success(kanban_home):
    staged = kb.stage_attachment_bytes(
        "screenshot.png", _PNG_1PX, content_type="image/png", uploaded_by="tester",
    )
    assert staged.token.startswith("st_")
    assert staged.filename == "screenshot.png"
    assert staged.content_type == "image/png"
    assert staged.size == len(_PNG_1PX)
    assert Path(staged.stored_path).read_bytes() == _PNG_1PX

    got = kb.get_staged_attachment(staged.token)
    assert got is not None
    assert got.token == staged.token


def test_stage_attachment_bytes_rejects_bad_mime(kanban_home):
    with pytest.raises(ValueError, match="unsupported image type"):
        kb.stage_attachment_bytes(
            "doc.pdf", b"%PDF-1.4", content_type="application/pdf",
        )


def test_stage_attachment_bytes_rejects_oversize(kanban_home):
    oversized = b"\x00" * (kb.KANBAN_IMAGE_ATTACHMENT_MAX_BYTES + 1)
    with pytest.raises(kb.AttachmentTooLarge):
        kb.stage_attachment_bytes(
            "big.png", oversized, content_type="image/png",
        )


def test_delete_staged_attachment(kanban_home):
    staged = kb.stage_attachment_bytes(
        "x.png", _PNG_1PX, content_type="image/png",
    )
    blob = Path(staged.stored_path)
    assert blob.exists()
    assert kb.delete_staged_attachment(staged.token) is True
    assert not blob.exists()
    assert kb.get_staged_attachment(staged.token) is None
    # Second delete of the same (now-gone) token is a no-op, not an error.
    assert kb.delete_staged_attachment(staged.token) is False


def test_promote_staged_attachments_roundtrip(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="paste test")
        staged = kb.stage_attachment_bytes(
            "shot.png", _PNG_1PX, content_type="image/png", uploaded_by="tester",
        )
        staged_path = Path(staged.stored_path)
        assert staged_path.exists()

        promoted, warnings = kb.promote_staged_attachments(
            conn, task_id, [staged.token], uploaded_by="dashboard",
        )
        assert warnings == []
        assert len(promoted) == 1
        att = promoted[0]
        assert att.task_id == task_id
        assert att.filename == "shot.png"
        assert att.content_type == "image/png"
        assert att.size == len(_PNG_1PX)

        # The blob moved (rename), not copied — the staged path is gone,
        # and the promoted attachment's stored_path holds the same bytes.
        assert not staged_path.exists()
        assert Path(att.stored_path).read_bytes() == _PNG_1PX
        assert Path(att.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(task_id).resolve()
        )

        # The staged row is gone; the real attachment is listed for the task.
        assert kb.get_staged_attachment(staged.token) is None
        atts = kb.list_attachments(conn, task_id)
        assert len(atts) == 1
        assert atts[0].id == att.id
    finally:
        conn.close()


def test_promote_staged_attachments_unknown_token_warns(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="paste test")
        promoted, warnings = kb.promote_staged_attachments(
            conn, task_id, ["st_doesnotexist"], uploaded_by="dashboard",
        )
        assert promoted == []
        assert len(warnings) == 1
        assert "not found" in warnings[0]
        # Task creation-adjacent call must not raise even on a stale token.
        assert kb.list_attachments(conn, task_id) == []
    finally:
        conn.close()


def test_promote_staged_attachments_caps_per_task(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="paste test")
        tokens = [
            kb.stage_attachment_bytes(
                f"img{i}.png", _PNG_1PX, content_type="image/png",
            ).token
            for i in range(kb.KANBAN_MAX_PENDING_ATTACHMENTS_PER_TASK + 2)
        ]
        promoted, warnings = kb.promote_staged_attachments(
            conn, task_id, tokens, uploaded_by="dashboard",
        )
        assert len(promoted) == kb.KANBAN_MAX_PENDING_ATTACHMENTS_PER_TASK
        assert len(warnings) == 2
        assert all("exceeds max" in w for w in warnings)
    finally:
        conn.close()


def test_reap_staged_attachments_removes_old_rows(kanban_home):
    staged = kb.stage_attachment_bytes(
        "old.png", _PNG_1PX, content_type="image/png",
    )
    blob = Path(staged.stored_path)
    assert blob.exists()

    # Backdate the row so it looks abandoned past the TTL.
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE staged_attachments SET created_at = ? WHERE token = ?",
                (int(time.time()) - 1000, staged.token),
            )
    finally:
        conn.close()

    removed = kb.reap_staged_attachments(max_age_seconds=100)
    assert removed == 1
    assert kb.get_staged_attachment(staged.token) is None
    assert not blob.exists()


def test_reap_staged_attachments_keeps_recent_rows(kanban_home):
    staged = kb.stage_attachment_bytes(
        "recent.png", _PNG_1PX, content_type="image/png",
    )
    removed = kb.reap_staged_attachments(max_age_seconds=kb.KANBAN_STAGED_ATTACHMENT_TTL_SECONDS)
    assert removed == 0
    assert kb.get_staged_attachment(staged.token) is not None


# ---------------------------------------------------------------------------
# REST surface — POST/GET/DELETE /attachments/staged
# ---------------------------------------------------------------------------


def test_upload_staged_attachment_success(client):
    r = client.post(
        "/api/plugins/kanban/attachments/staged",
        files={"file": ("paste.png", _PNG_1PX, "image/png")},
    )
    assert r.status_code == 201, r.text
    att = r.json()["attachment"]
    assert att["token"].startswith("st_")
    assert att["filename"] == "paste.png"
    assert att["content_type"] == "image/png"
    assert att["size"] == len(_PNG_1PX)
    # stored_path must not leak in the staged response.
    assert "stored_path" not in att


def test_upload_staged_attachment_rejects_invalid_type(client):
    r = client.post(
        "/api/plugins/kanban/attachments/staged",
        files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 400, r.text
    assert "unsupported image type" in r.json()["detail"]


def test_upload_staged_attachment_rejects_oversize(client):
    oversized = b"\x00" * (kb.KANBAN_IMAGE_ATTACHMENT_MAX_BYTES + 1024)
    r = client.post(
        "/api/plugins/kanban/attachments/staged",
        files={"file": ("big.png", oversized, "image/png")},
    )
    assert r.status_code == 413, r.text
    assert "MB limit" in r.json()["detail"]


def test_staged_download_and_delete_roundtrip(client):
    r = client.post(
        "/api/plugins/kanban/attachments/staged",
        files={"file": ("shot.gif", _PNG_1PX, "image/gif")},
    )
    token = r.json()["attachment"]["token"]

    r = client.get(f"/api/plugins/kanban/attachments/staged/{token}")
    assert r.status_code == 200
    assert r.content == _PNG_1PX

    r = client.delete(f"/api/plugins/kanban/attachments/staged/{token}")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "token": token}

    assert client.get(f"/api/plugins/kanban/attachments/staged/{token}").status_code == 404
    assert client.delete(f"/api/plugins/kanban/attachments/staged/{token}").status_code == 404


def test_staged_download_unknown_token_404(client):
    r = client.get("/api/plugins/kanban/attachments/staged/st_nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /tasks — pending_attachment_tokens promotion
# ---------------------------------------------------------------------------


def test_create_task_promotes_pending_attachment_tokens(client):
    r = client.post(
        "/api/plugins/kanban/attachments/staged",
        files={"file": ("shot.png", _PNG_1PX, "image/png")},
    )
    token = r.json()["attachment"]["token"]

    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "task with image", "pending_attachment_tokens": [token]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    task_id = body["task"]["id"]
    assert body["attachment_warnings"] == []
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["filename"] == "shot.png"
    assert body["attachments"][0]["content_type"] == "image/png"

    # The staged token is now consumed / gone.
    assert client.get(f"/api/plugins/kanban/attachments/staged/{token}").status_code == 404

    # GET /tasks/{id} surfaces the promoted attachment via the existing
    # attachments field (unchanged serializer/route).
    detail = client.get(f"/api/plugins/kanban/tasks/{task_id}").json()
    assert len(detail["attachments"]) == 1
    assert detail["attachments"][0]["filename"] == "shot.png"


def test_create_task_with_stale_token_warns_but_succeeds(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "task with stale token", "pending_attachment_tokens": ["st_stale"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"] is not None
    assert body["attachments"] == []
    assert len(body["attachment_warnings"]) == 1
    assert "not found" in body["attachment_warnings"][0]


def test_create_task_without_pending_tokens_field_is_unaffected(client):
    """Existing/older callers that never send the new field must be unaffected."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "legacy client task"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"] is not None
    assert body["attachments"] == []
    assert body["attachment_warnings"] == []
