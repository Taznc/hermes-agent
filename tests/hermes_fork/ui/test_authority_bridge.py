"""Plugin UI bridge on the gateway session authority (``plugin-ui-authority`` anchor).

Desktop chats run their turns on the session authority, not the TUI sidecar. Before the anchor, a plugin
tool's ``ui.request`` there returned None at once (no sidecar session owns the agent), so ``ask`` never
showed its card and always fell back to clarify. Driven through upstream's real ``local_desktop_ui.wire``
and ``session_server_requests`` round trip, with a fake viewer transport.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from gateway import local_desktop_ui, session_server_requests
from hermes_fork.ui.bridge import PluginUI
from tools import desktop_ui
from tools.process_registry import process_registry


class _Viewer:
    """An attached window: records frames and answers ``plugin.request`` with the queued answer."""

    def __init__(self):
        self.frames: list[dict] = []
        self.answers: list = []

    def transports(self):
        return [object()]

    def write(self, frame, **_kw):
        self.frames.append(frame)
        if frame.get("method") == "plugin.request" and self.answers:
            answer = self.answers.pop(0)
            if answer == "error":
                session_server_requests.resolve({"id": frame["id"], "error": {"code": -32601, "message": "x"}})
            elif answer != "never":
                session_server_requests.resolve({"id": frame["id"], "result": answer})
        return True


@pytest.fixture
def turn(monkeypatch):
    # Keep wire()'s process-wide installs inside the test.
    for name, value in (("_turns", {}), ("_process_routes", {}), ("_emitter_installed", False),
                        ("_process_sinks_installed", False)):
        monkeypatch.setattr(local_desktop_ui, name, value)
    from tools.connectors.operation import ConnectionOperation
    monkeypatch.setattr(ConnectionOperation, "on_change", None)
    monkeypatch.setattr(desktop_ui, "_emit", None)
    monkeypatch.setattr(process_registry, "on_output", None)
    monkeypatch.setattr(process_registry, "on_close", None)

    viewer, published, cancels = _Viewer(), [], []
    stream = SimpleNamespace(fanout=viewer,
                             publish=lambda sid, payload, *, event_type: cancels.append((event_type, payload)))
    authority = SimpleNamespace(sessions={"canon": SimpleNamespace(event_stream=stream)})
    runner = SimpleNamespace(
        _ctx=SimpleNamespace(session_id="phys", session_key="agent:main:local:dm:1"),
        _approval_owner=(authority, "canon", 1),
        _publish_execution=lambda kind, payload: published.append((kind, payload)) or True)
    agent = SimpleNamespace(_interrupt_requested=False)
    local_desktop_ui.wire(runner, agent)
    return SimpleNamespace(agent=agent, viewer=viewer, published=published, cancels=cancels, runner=runner)


def test_request_reaches_the_viewer_and_returns_its_answer(turn):
    turn.viewer.answers.append({"payload": {"answers": {"0": "yes"}}})
    ui = PluginUI("fork-ask", turn.agent)

    assert ui.available is True
    assert ui.request("fork-ask/questions", {"q": 1}, timeout=5) == {"payload": {"answers": {"0": "yes"}}}
    [frame] = [f for f in turn.viewer.frames if f.get("method") == "plugin.request"]
    assert frame["params"] == {"session_id": "canon", "kind": "fork-ask/questions", "payload": {"q": 1}}


def test_unsupported_is_returned_so_the_plugin_falls_back(turn):
    turn.viewer.answers.append({"unsupported": True})
    assert PluginUI("p", turn.agent).request("p/k", timeout=5) == {"unsupported": True}


def test_error_response_returns_none(turn):
    turn.viewer.answers.append("error")
    assert PluginUI("p", turn.agent).request("p/k", timeout=5) is None


def test_interrupt_cancels_a_blocked_request_and_closes_the_card(turn):
    turn.viewer.answers.append("never")
    ui = PluginUI("p", turn.agent)
    result: list = []
    worker = threading.Thread(target=lambda: result.append(ui.request("p/k", timeout=0)))
    worker.start()
    from tests.waiting import wait_until
    wait_until(lambda: any(f.get("method") == "plugin.request" for f in turn.viewer.frames))
    turn.agent._interrupt_requested = True
    worker.join(timeout=10)

    assert result == [None]
    assert ("request.cancel", {"id": turn.viewer.frames[-1]["id"], "method": "plugin.request",
                               "reason": "interrupted"}) in turn.cancels


def test_emit_publishes_plugin_event_on_the_turn(turn):
    assert PluginUI("p", turn.agent).emit("p/progress", {"n": 1}) is True
    assert turn.published[-1] == ("plugin.event", {"kind": "p/progress", "payload": {"n": 1}})


def test_agent_with_no_ui_turn_has_no_channel():
    ui = PluginUI("p", SimpleNamespace(_interrupt_requested=False))
    assert ui.available is False
    assert ui.request("p/k", timeout=1) is None
    assert ui.emit("p/k") is False
