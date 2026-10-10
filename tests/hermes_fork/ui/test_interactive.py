"""``interactive=True``: the ``interactive-plugin-tools`` anchor (agent/tool_dispatch_helpers.py) makes the tool a
batch barrier everywhere upstream reads ``_NEVER_PARALLEL_TOOLS``, and the lifecycle predicate rebound by the
``gateway-fork-methods`` anchor always emits its tool.start/complete."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from agent import tool_dispatch_helpers, tool_executor
from agent.tool_dispatch_helpers import _plan_tool_batch_segments
from hermes_cli import plugins as plugins_mod
from hermes_fork.ui import interactive

_UPSTREAM = frozenset({"clarify", "manage_connections", "manage_catalog", "setup_choose"})


def _tc(name, arguments="{}", call_id=None):
    return SimpleNamespace(id=call_id or f"call_{uuid.uuid4().hex[:8]}", type="function",
                           function=SimpleNamespace(name=name, arguments=arguments))


@pytest.fixture
def ctx(monkeypatch):
    manager = plugins_mod.PluginManager()
    context = plugins_mod.PluginContext(plugins_mod.PluginManifest(name="askp", key="askp", source="user"), manager)
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: manager)
    yield context
    manager.unload()


def _register(ctx, name, interactive_flag=True):
    schema = {"name": name, "description": "d", "parameters": {"type": "object", "properties": {}}}
    return ctx.fork.register_tool(name, "askp", schema, lambda args, **_: json.dumps({}),
                                  interactive=interactive_flag)


def test_anchor_wraps_the_set_tool_executor_imports_by_name():
    """tool_executor did ``from agent.tool_dispatch_helpers import _NEVER_PARALLEL_TOOLS``: same object."""
    assert tool_executor._NEVER_PARALLEL_TOOLS is tool_dispatch_helpers._NEVER_PARALLEL_TOOLS
    assert isinstance(tool_dispatch_helpers._NEVER_PARALLEL_TOOLS, interactive.NeverParallelTools)
    # Enumeration is upstream's set exactly; only ``in`` widens.
    assert frozenset(tool_dispatch_helpers._NEVER_PARALLEL_TOOLS) == _UPSTREAM
    assert interactive.with_interactive_plugin_tools(tool_dispatch_helpers._NEVER_PARALLEL_TOOLS) \
        is tool_dispatch_helpers._NEVER_PARALLEL_TOOLS


def test_interactive_tool_is_never_parallel_until_unloaded(ctx):
    assert "askp_q" not in tool_executor._NEVER_PARALLEL_TOOLS
    _register(ctx, "askp_q")
    assert "askp_q" in tool_executor._NEVER_PARALLEL_TOOLS
    assert "askp_q" in tool_dispatch_helpers._NEVER_PARALLEL_TOOLS
    assert "askp_q" not in frozenset(tool_dispatch_helpers._NEVER_PARALLEL_TOOLS)
    ctx._manager.unload()
    assert "askp_q" not in tool_executor._NEVER_PARALLEL_TOOLS


def test_non_interactive_fork_registration_is_not_marked(ctx):
    _register(ctx, "askp_plain", interactive_flag=False)
    assert "askp_plain" not in tool_executor._NEVER_PARALLEL_TOOLS


def test_rejected_registration_is_not_marked(ctx):
    """A shadowing attempt the stock register_tool refuses (returns None) must not mark the name."""
    from tools.registry import registry

    schema = {"name": "askp_builtin", "description": "d", "parameters": {"type": "object", "properties": {}}}
    registry.register(name="askp_builtin", toolset="core_probe", schema=schema, handler=lambda args, **_: "{}")
    try:
        assert _register(ctx, "askp_builtin") is None
        assert "askp_builtin" not in tool_executor._NEVER_PARALLEL_TOOLS
    finally:
        registry.deregister("askp_builtin")


# -- batch segmentation (extends tests/agent/test_tool_batch_segmentation.py for the fork seam) ----------


def test_interactive_plugin_tool_is_a_barrier_between_parallel_reads(ctx):
    _register(ctx, "askp_q")
    calls = [_tc("web_search", call_id="r1"), _tc("read_file", '{"path":"a.py"}', call_id="r2"),
             _tc("askp_q", call_id="ask"),
             _tc("web_search", call_id="r3"), _tc("web_extract", call_id="r4")]
    segments = _plan_tool_batch_segments(calls)
    assert [(kind, [c.id for c in batch]) for kind, batch in segments] == [
        ("parallel", ["r1", "r2"]), ("sequential", ["ask"]), ("parallel", ["r3", "r4"])]


def test_the_mark_itself_reaches_the_planner():
    """Control: an unknown plugin tool is a barrier anyway, so prove the planner reads the mark on a name
    that is otherwise parallel-safe (``web_search``): marked → barrier, unmarked → parallel again."""
    calls = [_tc("web_search", call_id="a"), _tc("web_search", call_id="b")]
    assert [k for k, _ in _plan_tool_batch_segments(calls)] == ["parallel"]
    interactive.mark_interactive("web_search", "probe")
    try:
        assert tool_dispatch_helpers._batch_admission(calls[0], None) is None
        assert [k for k, _ in _plan_tool_batch_segments(calls)] == ["sequential"]
    finally:
        interactive.unmark_interactive("web_search", "probe")
    assert [k for k, _ in _plan_tool_batch_segments(calls)] == ["parallel"]


# -- lifecycle predicate pin ------------------------------------------------------------------------------


def test_lifecycle_predicate_rebound_in_the_gateway_module(server, ctx):
    fn = server._tool_lifecycle_required_for_ui
    assert getattr(fn, "__fork_wrapped__", None) is not None, "gateway-fork-methods anchor must rebind it"
    # Upstream's own answers are preserved.
    assert fn("clarify") and fn("manage_connections") and fn("setup_mcp")
    assert not fn("read_file")
    assert not fn("askp_q")
    _register(ctx, "askp_q")
    assert fn("askp_q")
    # tool_progress's rebound functions resolve the name through server's globals (method_ctx.bind_module):
    # the predicate they call IS the rebound one.
    assert server._on_tool_start.__globals__["_tool_lifecycle_required_for_ui"] is fn
    assert server._on_tool_complete.__globals__["_tool_lifecycle_required_for_ui"] is fn
    # Re-running the anchor does not double-wrap.
    from hermes_fork.ui.gateway import install
    install(server)
    assert server._tool_lifecycle_required_for_ui is fn


def test_interactive_tool_emits_tool_start_even_with_progress_off(server, wire, ctx, monkeypatch):
    sid = "lifecycle-sid"
    monkeypatch.setitem(server._sessions, sid, {"session_key": "k", "tool_progress_mode": "off"})
    monkeypatch.setattr(server, "_session_tool_progress_mode", lambda _sid: "off")
    _register(ctx, "askp_q")
    server._on_tool_start(sid, "tc-1", "askp_q", {})
    server._on_tool_start(sid, "tc-2", "read_file", {"path": "x"})
    assert [f["params"]["payload"]["tool_id"] for f in wire.events("tool.start")] == ["tc-1"]
