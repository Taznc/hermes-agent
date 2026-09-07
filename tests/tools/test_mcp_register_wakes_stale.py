"""Parked MCP servers revive only after an intentional lifecycle request.

Repeated discovery runs (including cron ticks) must not reconnect a parked
server whose configuration is unchanged. A configuration change is an
intentional recovery request and wakes the retained task.
"""

import pytest


@pytest.mark.no_isolate
def test_register_wakes_stale_cached_server(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools import mcp_tool_discovery as _mcp_discovery

    woken: list[str] = []

    class _Event:
        def __init__(self, name):
            self._name = name

        def set(self):
            woken.append(self._name)

    class _Stale:
        session = None

        def __init__(self, name):
            self.name = name
            self._config = {"url": "http://127.0.0.1:9/mcp"}
            self._reconnect_event = _Event(name)
            self._registered_tool_names: list[str] = []

    class _Alive:
        session = object()

        def __init__(self, name):
            self.name = name
            self._reconnect_event = _Event(name)
            self._registered_tool_names = [f"{name}__tool"]

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    stale = _Stale("parked-srv")
    alive = _Alive("healthy-srv")
    monkeypatch.setitem(mcp_tool._servers, "parked-srv", stale)
    monkeypatch.setitem(mcp_tool._servers, "healthy-srv", alive)

    try:
        result = _mcp_discovery.register_mcp_servers({
            "parked-srv": {"url": "http://127.0.0.1:9/mcp"},
            "healthy-srv": {"url": "http://127.0.0.1:9/mcp"},
        })
        # Both cached → no new connections attempted; existing names returned.
        assert "healthy-srv__tool" in result
        # Repeated discovery with the same config does not override a parked
        # server's explicit-reconnect gate.
        assert woken == []

        _mcp_discovery.register_mcp_servers({
            "parked-srv": {"url": "http://127.0.0.1:10/mcp"},
            "healthy-srv": {"url": "http://127.0.0.1:9/mcp"},
        })
        assert woken == ["parked-srv"]
    finally:
        mcp_tool._servers.pop("parked-srv", None)
        mcp_tool._servers.pop("healthy-srv", None)
