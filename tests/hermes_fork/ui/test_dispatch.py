"""``plugin-ui-dispatch`` anchor (model_tools._execute_tool) + ``plugin-context-fork`` anchor (hermes_cli/plugins.py).

Plugin tool handlers get ``ui`` and ``clarify_callback``; built-in tools never do; a ``kind`` outside the
plugin's namespace is a tool error with zero wire traffic."""

from __future__ import annotations

import inspect
import json

import pytest

import model_tools
from hermes_cli import plugins as plugins_mod
from hermes_fork.plugin_context import ForkPluginContext
from hermes_fork.ui.bridge import PluginUI
from tools.registry import registry

_SCHEMA = {"name": "", "description": "probe", "parameters": {"type": "object", "properties": {}}}


def _schema(name):
    return {**_SCHEMA, "name": name}


@pytest.fixture
def plugin(monkeypatch):
    """A real PluginManager/PluginContext for plugin ``uiprobe``, served as the active manager."""
    manager = plugins_mod.PluginManager()
    manifest = plugins_mod.PluginManifest(name="uiprobe", key="uiprobe", source="user")
    ctx = plugins_mod.PluginContext(manifest, manager)
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: manager)
    handles = []
    yield ctx, handles
    for handle in reversed(handles):
        handle.dispose()


def _call(name, args=None):
    return json.loads(model_tools.handle_function_call(name, args or {}, task_id="t", tool_call_id="c1",
                                                       skip_pre_tool_call_hook=True))


def test_plugin_context_fork_is_additive():
    """``ctx.fork`` is the one addition; the stock ``register_tool`` signature is untouched."""
    params = list(inspect.signature(plugins_mod.PluginContext.register_tool).parameters)
    assert params == ["self", "name", "toolset", "schema", "handler", "check_fn", "requires_env",
                      "is_async", "description", "emoji", "override"]
    manager = plugins_mod.PluginManager()
    ctx = plugins_mod.PluginContext(plugins_mod.PluginManifest(name="p", key="p"), manager)
    assert isinstance(ctx.fork, ForkPluginContext)
    assert ctx.fork is ctx.fork
    assert isinstance(ctx.fork.ui, PluginUI) and ctx.fork.ui.plugin_id == "p"


def test_plugin_tool_receives_ui_and_clarify_callback(plugin, live_turn, wire):
    ctx, handles = plugin
    seen = {}

    def handler(args, ui=None, clarify_callback=None, **_):
        seen.update(ui=ui, clarify_callback=clarify_callback)
        return json.dumps({"ok": True})

    handles.append(ctx.fork.register_tool("uiprobe_tool", "uiprobe", _schema("uiprobe_tool"), handler))
    assert _call("uiprobe_tool") == {"ok": True}
    assert isinstance(seen["ui"], PluginUI) and seen["ui"].plugin_id == "uiprobe"
    assert seen["clarify_callback"] is live_turn.agent.clarify_callback
    assert seen["ui"].available is True
    assert wire.frames == []  # receiving the kwargs sends nothing


def test_narrow_plugin_handler_is_not_broken(plugin, live_turn):
    """``registry.dispatch`` signature-filters kwargs: a ``handle(args)`` plugin tool keeps working."""
    ctx, handles = plugin
    handles.append(ctx.register_tool("uiprobe_narrow", "uiprobe", _schema("uiprobe_narrow"),
                                     lambda args: json.dumps({"narrow": True})))
    assert _call("uiprobe_narrow") == {"narrow": True}


def test_stock_tool_never_receives_ui_or_clarify_callback(live_turn, monkeypatch):
    seen = {}

    def handler(args, **kwargs):
        seen.update(kwargs)
        return json.dumps({"stock": True})

    registry.register(name="uiprobe_stock", toolset="uiprobe_stock", schema=_schema("uiprobe_stock"),
                      handler=handler)
    try:
        assert _call("uiprobe_stock") == {"stock": True}
    finally:
        registry.deregister("uiprobe_stock")
    assert "ui" not in seen and "clarify_callback" not in seen
    assert "task_id" in seen  # it did get upstream's kwargs: the filter is ownership, not signature


def test_kind_outside_plugin_namespace_is_a_tool_error_with_no_wire_traffic(plugin, live_turn, wire):
    ctx, handles = plugin

    def handler(args, ui=None, **_):
        ui.request("otherplugin/steal", {"x": 1})
        return json.dumps({"unreachable": True})

    handles.append(ctx.fork.register_tool("uiprobe_bad", "uiprobe", _schema("uiprobe_bad"), handler))
    result = _call("uiprobe_bad")
    assert "error" in result and "uiprobe/<name>" in result["error"]
    assert wire.frames == []


def test_plugin_ui_request_round_trips_through_dispatch(plugin, live_turn, wire):
    ctx, handles = plugin
    wire.answer({"payload": {"picked": 2}})

    def handler(args, ui=None, **_):
        return json.dumps({"answer": ui.request("uiprobe/pick", {"options": [1, 2, 3]}, timeout=5)})

    handles.append(ctx.fork.register_tool("uiprobe_ask", "uiprobe", _schema("uiprobe_ask"), handler))
    assert _call("uiprobe_ask") == {"answer": {"payload": {"picked": 2}}}
    [frame] = wire.requests()
    assert frame["params"] == {"session_id": live_turn.sid, "kind": "uiprobe/pick", "payload": {"options": [1, 2, 3]}}


def test_unowned_tool_name_after_unload_gets_nothing(plugin, live_turn):
    ctx, handles = plugin
    seen = {}

    def handler(args, **kwargs):
        seen.update(kwargs)
        return "{}"

    handle = ctx.fork.register_tool("uiprobe_gone", "uiprobe", _schema("uiprobe_gone"), handler)
    from hermes_fork.ui.dispatch import plugin_owner
    assert plugin_owner("uiprobe_gone") == "uiprobe"
    handle.dispose()
    assert plugin_owner("uiprobe_gone") is None
