"""Shared fixtures for the plugin UI bridge tests: the real gateway module, a live session whose agent is
bound as the turn's parent (exactly what ``turn_facade`` does), and a frame recorder standing in for the
client transport."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def server(monkeypatch):
    from hermes_cli import banner

    # The real server binds callbacks on import; isolate only its process-wide side effects.
    monkeypatch.setattr(banner, "prefetch_update_check", lambda: None)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    from tui_gateway import server as mod
    from tui_gateway import server_requests

    server_requests.reset_for_tests()
    yield mod
    server_requests.reset_for_tests()


class Wire:
    """Records every frame the gateway writes; ``answer`` settles the next server request with a result."""

    def __init__(self, server_requests):
        self.frames: list[dict] = []
        self._answers: list = []
        self._server_requests = server_requests

    def answer(self, result):
        self._answers.append(result)

    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        if isinstance(frame.get("id"), str) and "method" in frame and self._answers:
            result = self._answers.pop(0)
            if result == "cancel":
                self._server_requests.cancel(frame["params"]["session_id"])
            elif isinstance(result, dict) and "error" in result:
                self._server_requests.resolve_response({"jsonrpc": "2.0", "id": frame["id"], **result})
            elif result != "never":
                self._server_requests.resolve_response({"jsonrpc": "2.0", "id": frame["id"], "result": result})
        return True

    def requests(self, method: str = "plugin.request") -> list[dict]:
        return [f for f in self.frames if f.get("method") == method]

    def events(self, type_: str = "plugin.event") -> list[dict]:
        return [f for f in self.frames if f.get("method") == "event" and f["params"]["type"] == type_]


@pytest.fixture
def wire(server, monkeypatch):
    from tui_gateway import server_requests

    w = Wire(server_requests)
    monkeypatch.setattr(server, "write_json", w.write)
    return w


@pytest.fixture
def live_turn(server, monkeypatch):
    """A live UI session ``ui-sid`` whose agent is bound as this context's turn parent."""
    from agent.subagent_lifecycle import bind_subagent_parent

    agent = SimpleNamespace(clarify_callback=lambda *a, **k: "clarified")
    sid = "ui-sid"
    monkeypatch.setitem(server._sessions, sid, {"session_key": "sk", "agent": agent, "source": "desktop"})
    tokens = server._set_session_context("sk", ui_session_id=sid)
    try:
        with bind_subagent_parent(agent):
            yield SimpleNamespace(sid=sid, agent=agent)
    finally:
        server._clear_session_context(tokens)
