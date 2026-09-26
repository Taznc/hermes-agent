"""Gateway-level round-trip for the propose_new_session blocking bridge.

Mirrors the existing coverage for mcp.setup.request/respond (test_protocol.py's
parametrized sensitive-prompt tests): session.propose.request must expire cleanly on
timeout, session.propose.respond must tolerate a late reply, and a normal round-trip
must deliver the renderer's answer back to the blocked caller.
"""

import json
import threading
import time

import pytest


@pytest.fixture()
def server():
    from unittest.mock import MagicMock, patch

    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")

    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    for sid in list(mod._sessions):
        mod._close_session_by_id(sid, end_reason="test_cleanup")
    mod._pending.clear()
    mod._answers.clear()
    mod._live_transports.clear()


@pytest.fixture()
def capture(server):
    import io
    buf = io.StringIO()
    server._real_stdout = buf
    return server, buf


def test_session_propose_is_in_expiring_requests(server):
    assert "session.propose.request" in server._EXPIRING_REQUESTS


def test_session_propose_timeout_emits_expiry(capture):
    server, buf = capture

    assert server._block("session.propose.request", "s1", {"topic": "New topic"}, timeout=0) == ""

    messages = [json.loads(line) for line in buf.getvalue().splitlines()]
    request, expiry = [message["params"] for message in messages]
    assert request["type"] == "session.propose.request"
    assert expiry["type"] == "session.propose.expire"
    assert expiry["session_id"] == "s1"
    assert expiry["payload"]["request_id"] == request["payload"]["request_id"]


def test_session_propose_late_response_is_idempotent(server):
    response = server.handle_request({
        "id": "late-response", "method": "session.propose.respond",
        "params": {"request_id": "expired-request", "result": ""},
    })
    assert response["result"] == {"status": "expired"}


def test_session_propose_round_trip_delivers_renderer_answer(server):
    result = [None]

    def run():
        result[0] = server._block("session.propose.request", "s1", {"topic": "New topic"}, timeout=5)

    thread = threading.Thread(target=run)
    thread.start()

    deadline = time.monotonic() + 2
    rid = None
    while time.monotonic() < deadline and rid is None:
        with server._prompt_lock:
            rid = next(iter(server._pending), None)
        time.sleep(0.01)
    assert rid

    outcome = json.dumps({"status": "approved", "session_id": "new-sid"})
    response = server.handle_request({
        "id": "a1", "method": "session.propose.respond",
        "params": {"request_id": rid, "result": outcome},
    })
    assert response["result"]["status"] == "ok"

    thread.join(timeout=5)
    assert result[0] == outcome
