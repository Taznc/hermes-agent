"""Gateway half: ``plugin-ui-bridge`` anchor (tui_gateway/agent_callbacks.py) + contracts declared at
registration by the existing ``gateway-fork-methods`` anchor."""

from __future__ import annotations

import sys

import pytest

from hermes_fork.ui import gateway as ui_gateway
from hermes_fork.ui.bridge import PluginUI


def test_anchor_exposes_the_bridge_callbacks(server):
    from tui_gateway import agent_callbacks

    assert agent_callbacks.ui_request_callback is ui_gateway.ui_request_callback
    assert agent_callbacks.ui_emit_callback is ui_gateway.ui_emit_callback
    assert ui_gateway._server is server


def test_contracts_declared_at_registration_and_kept_out_of_generated_files(server):
    from hermes_fork.gateway.registry import upstream_only
    from tui_gateway.contracts import registry

    assert "plugin.request" in registry.SERVER_REQUESTS
    assert "plugin.event" in registry.EVENTS
    assert "plugin.request" not in upstream_only(registry.SERVER_REQUESTS)
    assert "plugin.event" not in upstream_only(registry.EVENTS)
    assert "clarify" in upstream_only(registry.SERVER_REQUESTS)
    contract = registry.SERVER_REQUESTS["plugin.request"]
    assert contract.result.model_validate({"unsupported": True}).unsupported is True
    assert contract.result.model_validate({"payload": {"a": [1]}}).payload == {"a": [1]}
    _, problem = registry.validate_params(contract, {"session_id": "s", "kind": "p/k", "payload": 1, "bogus": 1})
    assert problem and "bogus" in problem


def test_request_answers_payload(server, wire, live_turn):
    wire.answer({"payload": {"ok": 1}})
    assert PluginUI("p").request("p/k", {"q": 1}, timeout=5) == {"payload": {"ok": 1}}
    [frame] = wire.requests()
    assert frame["params"] == {"session_id": live_turn.sid, "kind": "p/k", "payload": {"q": 1}}


def test_request_unsupported_is_returned_for_the_plugin_to_fall_back(server, wire, live_turn):
    wire.answer({"unsupported": True})
    assert PluginUI("p").request("p/k", timeout=5) == {"unsupported": True}


@pytest.mark.parametrize("answer", ["cancel", {"error": {"code": -32601, "message": "no handler"}}])
def test_cancel_and_error_response_return_none(server, wire, live_turn, answer):
    wire.answer(answer)
    assert PluginUI("p").request("p/k", timeout=5) is None


def test_interrupt_cancels_a_blocked_request(server, wire, live_turn):
    """``session.interrupt`` → ``_clear_pending(sid)`` → ``server_requests.cancel``: request returns None."""
    import threading

    from tui_gateway import server_requests

    wire.answer("never")
    out = {}
    ui = PluginUI("p", live_turn.agent)
    t = threading.Thread(target=lambda: out.setdefault("r", ui.request("p/k", timeout=0)))
    t.start()
    for _ in range(200):
        if server_requests.open_requests(live_turn.sid):
            break
        threading.Event().wait(0.01)
    assert server_requests.pending_kind(live_turn.sid) == "plugin.request"
    server._clear_pending(live_turn.sid)
    t.join(5)
    assert not t.is_alive() and out["r"] is None
    assert [e["params"]["payload"]["reason"] for e in wire.events("request.cancel")] == ["interrupted"]


@pytest.mark.parametrize("configured,expected", [(0, None), (-1, None), (42, 42.0)])
def test_default_timeout_is_agent_clarify_timeout(server, live_turn, monkeypatch, configured, expected):
    from tools import clarify_gateway
    from tui_gateway import server_requests

    monkeypatch.setattr(clarify_gateway, "get_clarify_timeout", lambda: configured)
    calls = []
    monkeypatch.setattr(server_requests, "send", lambda method, sid, params, *, timeout, **_: (
        calls.append((method, timeout)) or {"payload": None}))
    PluginUI("p").request("p/k")
    PluginUI("p").request("p/k", timeout=3)
    PluginUI("p").request("p/k", timeout=0)
    assert calls == [("plugin.request", expected), ("plugin.request", 3.0), ("plugin.request", None)]


def test_emit_writes_plugin_event(server, wire, live_turn):
    assert PluginUI("p").emit("p/progress", {"pct": 40}) is True
    [frame] = wire.events()
    assert frame["params"]["session_id"] == live_turn.sid
    assert frame["params"]["payload"] == {"kind": "p/progress", "payload": {"pct": 40}}


def test_no_live_ui_for_the_turn_means_none_and_no_traffic(server, wire, live_turn):
    """A delegated child / side agent (not the session's agent) never raises a card."""
    child = PluginUI("p", object())
    assert child.available is False
    assert child.request("p/k", timeout=5) is None
    assert child.emit("p/k") is False
    assert wire.frames == []


def test_cli_process_without_the_gateway_returns_none(monkeypatch, live_turn, wire):
    monkeypatch.delitem(sys.modules, "hermes_fork.ui.gateway")
    assert PluginUI("p").request("p/k", timeout=5) is None
    assert PluginUI("p").emit("p/k") is False
    assert wire.frames == []


@pytest.mark.parametrize("kind", ["other/k", "p/", "p", "", None, "pk/x"])
def test_kind_must_be_namespaced(kind, wire, live_turn):
    with pytest.raises(ValueError, match="p/<name>"):
        PluginUI("p").request(kind)
    with pytest.raises(ValueError, match="p/<name>"):
        PluginUI("p").emit(kind)
    assert wire.frames == []


def test_non_json_payload_is_rejected_before_the_wire(wire, live_turn):
    with pytest.raises(ValueError, match="JSON"):
        PluginUI("p").request("p/k", {"x": object()})
    assert wire.frames == []
