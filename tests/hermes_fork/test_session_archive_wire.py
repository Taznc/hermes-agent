"""Wire-level archive refusal and atomic compound mutation regressions."""

import os
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient as AioClient
from aiohttp.test_utils import TestServer
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("parent", "desktop")
    database.set_session_title("parent", "original")
    yield database
    database.close()


def busy(db):
    assert db.try_acquire_session_turn_lease(
        "parent", f"pid={os.getpid()}:turn=wire-test"
    )


def assert_original(db):
    row = db.get_session("parent")
    assert row["title"] == "original"
    assert not row["archived"] and not row["hidden"] and not row["pinned"]
    assert row["last_read_at"] is None


def test_rest_compound_patch_rejected_without_any_mutation(db, monkeypatch):
    from hermes_cli.web_routers import sessions

    monkeypatch.setattr(sessions, "_with_db", lambda profile, fn, **kwargs: fn(db))
    app = FastAPI()
    app.include_router(sessions.manage_router)
    busy(db)
    with TestClient(app) as client:
        response = client.patch(
            "/api/sessions/parent",
            json={
                "title": "changed",
                "archived": True,
                "hidden": True,
                "pinned": True,
                "unread": True,
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_archive_blocked"
    assert_original(db)


@pytest.mark.asyncio
async def test_messaging_compound_patch_rejected_without_any_mutation(db):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    app = web.Application()
    app.router.add_patch("/api/sessions/{session_id}", adapter._handle_patch_session)
    busy(db)
    async with AioClient(TestServer(app)) as client:
        response = await client.patch(
            "/api/sessions/parent",
            json={
                "title": "changed",
                "archived": True,
                "hidden": True,
                "pinned": True,
                "unread": True,
            },
        )
        assert response.status == 409
        payload = await response.json()
        assert payload["error"]["code"] == "session_archive_blocked"
    assert_original(db)


def test_rpc_busy_draft_does_not_defer_archive(db, monkeypatch):
    import tui_gateway.server as srv

    monkeypatch.setattr(srv, "_get_db", lambda: db)
    record = {
        "session_key": "draft",
        "running": True,
        "history_lock": threading.RLock(),
        "agent": SimpleNamespace(session_id="draft", _session_db=db),
    }
    monkeypatch.setattr(srv, "_sessions", {"live-draft": record})
    response = srv._methods["session.archive"](
        1, {"session_id": "live-draft", "archived": True}
    )
    assert response["error"]["code"] == 4024
    assert "pending_archived" not in record


def test_rpc_durable_turn_returns_actionable_blocker(db, monkeypatch):
    import tui_gateway.server as srv

    monkeypatch.setattr(srv, "_get_db", lambda: db)
    monkeypatch.setattr(srv, "_sessions", {})
    busy(db)
    response = srv._methods["session.archive"](
        1, {"session_id": "parent", "archived": True}
    )
    assert response["error"]["code"] == 4024
    assert "active owned work" in response["error"]["message"]
    assert_original(db)
