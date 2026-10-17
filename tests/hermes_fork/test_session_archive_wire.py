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


def _authority_patch(db, payload, request_id="sidebar-1"):
    """The canonical seam upstream routes REST, gateway-owner and offline edits through."""
    import hermes_state_runtime as rt

    epoch = rt.begin_runtime_epoch(db, instance_id="owner")
    revision = db.get_session("parent")["runtime_revision"]
    return rt.mutate_runtime_session(
        db, epoch=epoch, principal_id="human", session_id="parent", request_id=request_id,
        expected_revision=revision, operation="sidebar", payload=payload,
    )


def test_authority_compound_patch_rejected_without_any_mutation(db):
    from hermes_state_runtime import RuntimeStoreError

    busy(db)
    with pytest.raises(RuntimeStoreError) as refused:
        _authority_patch(db, {"title": "changed", "archived": True, "hidden": True,
                              "pinned": True, "unread": True})
    assert refused.value.reason == "session_archive_blocked"
    assert_original(db)


def test_authority_archive_commits_when_idle_and_non_archive_edits_skip_the_guard(db):
    busy(db)
    # A busy session still takes a non-archive sidebar edit (only archiving is admission-gated).
    assert _authority_patch(db, {"pinned": True}, request_id="pin")["pinned"] is True
    db.release_session_turn_lease("parent", f"pid={os.getpid()}:turn=wire-test")
    assert _authority_patch(db, {"archived": True}, request_id="arch")["archived"] is True
    assert db.get_session("parent")["archived"]


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
