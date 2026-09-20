"""A running turn retains its generation, not its disconnected request socket."""

from types import SimpleNamespace


def test_child_spawn_after_reconnect_captures_live_generation_authority(monkeypatch):
    from tools import delegate_tool_registry as registry
    from tools.delegate_tool_child_run import _register_child
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport

    old = SimpleNamespace(write=lambda frame: True)
    new = SimpleNamespace(write=lambda frame: True)
    owner = {"transport": old, "session_key": "stored-parent", "history": []}
    monkeypatch.setattr(server, "_sessions", {"parent": owner})
    monkeypatch.setattr(registry, "_active_subagents", {})
    transport_token = bind_transport(old)
    generation_token = server._current_runtime_session_record.set(owner)
    try:
        # A long parent tool turn keeps the old ContextVar after its viewer reconnects.
        owner["transport"] = server._detached_ws_transport
        server._attach_session_transport(owner, new)
        via, record = registry._capture_gateway_steer_authority("parent")
        assert via is owner["transport"] and record is owner
        child = SimpleNamespace(_subagent_id="child", _delegate_depth=1, model="test")
        _register_child(child, None, "late spawn", owner_session_id="parent",
                        owner_transport=via, owner_session_record=record)
        reply = server.dispatch({"id": 1, "method": "subagent.list", "params": {"session_id": "parent"}}, transport=new)
        assert [r["subagent_id"] for r in reply["result"]["subagents"]] == ["child"]
        # The old RPC peer cannot borrow a running turn's internal generation authority.
        assert server._current_session_steer_authority("parent") == (None, None)
        server._sessions["parent"] = {**owner}
        assert registry._capture_gateway_steer_authority("parent") == (None, None)
    finally:
        server._current_runtime_session_record.reset(generation_token)
        reset_transport(transport_token)
