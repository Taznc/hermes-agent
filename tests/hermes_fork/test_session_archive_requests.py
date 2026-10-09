"""Exact request/mailbox ownership and profile-scoped RPC alias regression tests."""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "desktop")
    yield db
    db.close()


def test_plugin_ui_waiting_request_blocks_idle_parent(db, monkeypatch):
    import tui_gateway.server as srv
    from tui_gateway import server_requests

    parent = {
        "session_key": "parent",
        "profile_home": str(db.db_path.parent),
        "history_lock": threading.RLock(),
        "agent": SimpleNamespace(session_id="parent", _session_db=db),
    }
    monkeypatch.setattr(srv, "_sessions", {"live": parent})
    request = server_requests.ServerRequest(
        "live", "plugin.request", {"kind": "select"}
    )
    monkeypatch.setattr(server_requests, "_open", {request.id: request})
    with pytest.raises(ValueError, match="waiting_request"):
        db.set_session_archived("parent", True)
    assert not request.answered and not request.event.is_set()


def test_mailbox_queued_work_blocks_without_running_parent(db):
    from tools.bot_live_delivery import deliver_to_live_owner, read_delivery_result

    home = db.db_path.parent
    owner = {
        "profile_home": str(home.resolve()),
        "session_id": "parent",
        "live_session_id": "old-live",
        "lease_id": "lease-test",
    }
    ticket = deliver_to_live_owner(home, owner, "pending work")
    with pytest.raises(ValueError, match="pending_mailbox_delivery"):
        db.set_session_archived("parent", True)
    assert read_delivery_result(home, ticket["delivery_id"])["status"] == "queued"


def test_rpc_foreign_live_id_cannot_redirect_or_block_requested_profile(
    db, monkeypatch
):
    import tui_gateway.server as srv

    foreign = {
        "session_key": "foreign-parent",
        "profile_home": str(db.db_path.parent / "foreign"),
        "running": True,
    }
    monkeypatch.setattr(srv, "_sessions", {"parent": foreign})
    monkeypatch.setattr(srv, "_profile_home", lambda name: db.db_path.parent)

    @contextmanager
    def profile_db(params, *, writer=False):
        yield db

    monkeypatch.setattr(srv, "_profile_db", profile_db)
    response = srv._methods["session.archive"](
        1, {"session_id": "parent", "profile": "own", "archived": True}
    )
    assert response.get("result", {}).get("archived") is True, response
    assert db.get_session("parent")["archived"] == 1
    assert foreign["running"] and "pending_archived" not in foreign
