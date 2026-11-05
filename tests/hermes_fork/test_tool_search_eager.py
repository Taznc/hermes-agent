"""``tools.tool_search.eager`` (``tool-search-eager`` anchor in ``tools/tool_search.py``): a listed plugin
or MCP tool stays in the model-visible array instead of hiding behind the tool-search bridge."""

from __future__ import annotations

import logging

import pytest
import json

from hermes_fork import tool_search_eager
from tools import tool_search
from tools.registry import registry


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tool_search_eager.reset_for_tests()
    yield tmp_path
    tool_search_eager.reset_for_tests()


def _write(home, tool_search_cfg):
    (home / "config.yaml").write_text(json.dumps({"tools": {"tool_search": tool_search_cfg}}))


@pytest.fixture
def plugin_tools():
    names = {"fork_eager_probe_ask": "forkeagerprobe", "mcp_fork_eager_probe_op": "mcp-fork-eager-probe"}
    for name, toolset in names.items():
        registry.register(name=name, toolset=toolset,
                          schema={"name": name, "description": "probe", "parameters": {"type": "object"}},
                          handler=lambda args, **kw: "{}")
    yield list(names)
    for name in names:
        registry.deregister(name)


def _defs(names):
    return [{"type": "function", "function": {"name": n, "description": "probe"}} for n in names]


def test_plugin_and_mcp_tools_defer_without_the_setting(home, plugin_tools):
    _write(home, {})
    visible, deferrable = tool_search.classify_tools(_defs(plugin_tools))
    assert [d["function"]["name"] for d in deferrable] == plugin_tools
    assert visible == []


def test_eager_pins_a_plugin_tool_visible(home, plugin_tools):
    _write(home, {"eager": ["fork_eager_probe_ask"]})
    visible, deferrable = tool_search.classify_tools(_defs(plugin_tools))
    assert [d["function"]["name"] for d in visible] == ["fork_eager_probe_ask"]
    assert [d["function"]["name"] for d in deferrable] == ["mcp_fork_eager_probe_op"]


def test_eager_wins_over_an_explicit_defer_entry(home, plugin_tools):
    _write(home, {"eager": ["mcp_fork_eager_probe_op"], "defer": ["mcp_fork_eager_probe_op"]})
    assert tool_search.is_deferrable_tool_name("mcp_fork_eager_probe_op", frozenset({"mcp_fork_eager_probe_op"})) is False


def test_a_config_edit_is_picked_up_without_restart(home, plugin_tools):
    _write(home, {})
    assert tool_search.is_deferrable_tool_name("fork_eager_probe_ask") is True
    _write(home, {"eager": ["fork_eager_probe_ask"]})
    import os
    st = os.stat(home / "config.yaml")
    os.utime(home / "config.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert tool_search.is_deferrable_tool_name("fork_eager_probe_ask") is False


@pytest.mark.parametrize("value", ["ask", 3, {"ask": True}])
def test_malformed_value_is_ignored_loudly(home, plugin_tools, caplog, value):
    _write(home, {"eager": value})
    with caplog.at_level(logging.WARNING, logger="hermes_fork.tool_search_eager"):
        assert tool_search.is_deferrable_tool_name("fork_eager_probe_ask") is True
    assert "tools.tool_search.eager" in caplog.text


def test_parse_strips_blank_names():
    assert tool_search_eager.parse({"eager": [" ask ", ""]}) == frozenset({"ask"})
    assert tool_search_eager.parse(None) == frozenset()
