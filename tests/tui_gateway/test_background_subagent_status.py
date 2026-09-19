"""A settled parent turn can still own live background work."""

from types import SimpleNamespace


def test_live_session_status_tracks_background_children_not_just_parent_turn(monkeypatch):
    from tools import delegate_tool_registry as registry
    from tools.delegate_tool_child_run import _register_child
    from tui_gateway import server

    transport = SimpleNamespace(write=lambda frame: True)
    owner = {"session_key": "stored-parent", "running": False, "history": [], "transport": transport}
    monkeypatch.setattr(server, "_sessions", {"parent": owner})
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_session_live_title", lambda *args: "Background parent")
    monkeypatch.setattr(server, "_resolve_model", lambda: "test")
    monkeypatch.setattr(registry, "_active_subagents", {})
    monkeypatch.setattr(registry, "_recent_subagents", {})

    def status():
        # Both session.active_list and the HTTP Agents overview use this projection.
        return server._session_live_item("parent", owner)["status"]

    def register(sid, record=owner, owner_id="parent", via=transport):
        child = SimpleNamespace(_subagent_id=sid, _delegate_depth=1, model="test")
        _register_child(child, None, "background work", owner_session_id=owner_id,
                        owner_transport=via, owner_session_record=record)

    assert status() == "idle"
    register("retired", record={**owner})
    register("foreign", owner_id="other")
    register("uncommissioned", via=None)
    assert status() == "idle"
    register("child")
    assert status() == "working"
    assert owner["running"] is False  # Do not lock the parent composer / claim a live turn.

    monkeypatch.setattr(server, "_session_pending_kind", lambda sid: "clarify")
    assert status() == "waiting"
    monkeypatch.setattr(server, "_session_pending_kind", lambda sid: "")
    registry._unregister_subagent("child")
    assert status() == "idle"
