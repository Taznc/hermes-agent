"""Tests for POST /api/chat/file-upload (browser +Files picker / OS file drop).

Fork-owned: the endpoint itself is mounted from hermes_cli/web_routers/files.py
(an upstream-shared facade), but exercising it needs nothing upstream owns, so
the tests live here rather than in tests/hermes_cli/test_web_server.py — see
docs/fork-anchor-extraction.md rule 10 (a fork feature gets a fork-owned test).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_post_chat_file_upload_requires_auth(client):
    from hermes_cli.web_server import _SESSION_HEADER_NAME

    resp = client.post(
        "/api/chat/file-upload",
        json={"data_url": "data:text/plain;base64,aGVsbG8=", "filename": "notes.txt"},
        headers={_SESSION_HEADER_NAME: "wrong-token"},
    )
    assert resp.status_code == 401


def test_post_chat_file_upload_stages_bytes_under_uploads_dir(client):
    from hermes_constants import get_hermes_home

    resp = client.post(
        "/api/chat/file-upload",
        json={"data_url": "data:text/plain;base64,aGVsbG8gd29ybGQ=", "filename": "notes.txt"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["bytes"] == len(b"hello world")

    stored = Path(data["path"])
    assert stored.exists()
    assert stored.read_bytes() == b"hello world"
    assert stored.parent == get_hermes_home() / "uploads"
    # Filename is sanitized/namespaced, but the original stem + extension
    # survive so the staged file is still recognizable.
    assert stored.name.endswith("_notes.txt")


def test_post_chat_file_upload_sanitizes_path_traversal_in_filename(client):
    resp = client.post(
        "/api/chat/file-upload",
        json={"data_url": "data:text/plain;base64,eA==", "filename": "../../etc/passwd"},
    )

    assert resp.status_code == 200
    stored = Path(resp.json()["path"])
    # Only the basename survives; no path components from the client name
    # can escape HERMES_HOME/uploads/.
    assert stored.parent.name == "uploads"
    assert ".." not in stored.name


def test_post_chat_file_upload_rejects_oversized_payload(client):
    from hermes_cli import web_server as ws

    oversized = b"x" * (ws._CHAT_FILE_UPLOAD_MAX_BYTES + 1)

    resp = client.post(
        "/api/chat/file-upload",
        json={
            "data_url": f"data:application/octet-stream;base64,{base64.b64encode(oversized).decode()}",
            "filename": "big.bin",
        },
    )

    assert resp.status_code == 413


def test_post_chat_file_upload_rejects_non_data_url_payload(client):
    resp = client.post(
        "/api/chat/file-upload",
        json={"data_url": "not-a-data-url", "filename": "notes.txt"},
    )

    assert resp.status_code == 400
