"""Tests for parked-server revival.

Parking deregisters a server's tools. A genuinely parked server must remain
dormant until an explicit lifecycle request sets ``_reconnect_event``; it must
not turn a remote outage into an indefinite background reconnect loop.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_revival_discovery_registers_tools_while_ready_is_cleared(monkeypatch):
    """A managed server revival must publish tools before readiness is reset."""
    from tools import mcp_tool
    from tools import mcp_tool_registration as _mcp_registration
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("srv")
    server._config = {"url": "https://example.test/mcp"}
    server.session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[SimpleNamespace(name="send_message")],
            )
        )
    )
    server._ready.clear()
    server._registered_tool_names = []
    monkeypatch.setitem(mcp_tool._servers, server.name, server)

    register = MagicMock(return_value=["srv__send_message"])
    monkeypatch.setattr(_mcp_registration, "_register_server_tools", register)

    asyncio.run(server._discover_tools())

    register.assert_called_once_with(server.name, server, server._config)
    assert server._registered_tool_names == ["srv__send_message"]


@pytest.mark.no_isolate
def test_parked_server_waits_for_explicit_reconnect_before_revival(monkeypatch, tmp_path):
    """A parked server makes no automatic reconnect attempts, but a manual signal revives it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool_loop import reconnect_mcp_server
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_MAX_RECONNECT_RETRIES", 1)
    # The base implementation self-probes after 300s.  Override that old
    # scheduler seam so this contract fails quickly on base, while
    # ``raising=False`` also supports the new implementation which removed it.
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.05, raising=False)
    _real_sleep = asyncio.sleep

    async def _fast_sleep(_delay, *a, **kw):
        await _real_sleep(0)

    monkeypatch.setattr(mcp_tool.asyncio, "sleep", _fast_sleep)

    state = {
        "transport_calls": 0,
        "deregistered": 0,
        "backend_up": False,
        "revived_registration": 0,
    }

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            def _deregister_tools(self):
                state["deregistered"] += 1
                self._registered_tool_names = []

            def _register_discovered_tools_if_needed(self):
                if self._ready.is_set() and not self._registered_tool_names:
                    state["revived_registration"] += 1
                    self._registered_tool_names = ["srv__tool"]

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                if state["transport_calls"] == 1:
                    # First connect succeeds (sets _ready), then dies.
                    self.session = object()
                    self._ready.set()
                    self._ever_connected = True
                    self.session = None
                    raise RuntimeError("backend outage begins")
                if not state["backend_up"]:
                    raise RuntimeError("backend still down")
                # Backend recovered: establish a session and park in the
                # lifecycle wait like the real transport does.
                self.session = object()
                self._register_discovered_tools_if_needed()
                await self._wait_for_lifecycle_event()

        task = _Task("srv")
        task._registered_tool_names = ["srv__tool"]
        monkeypatch.setitem(mcp_tool._servers, task.name, task)

        run_task = asyncio.ensure_future(task.run({"command": "x"}))

        # Let it exhaust the budget (1 retry) and park.
        for _ in range(2000):
            await _real_sleep(0)
            if state["deregistered"] >= 1:
                break
        assert state["deregistered"] >= 1, "server never parked"
        assert not run_task.done(), "run task exited instead of parking"

        # The backend comes back, but no operator, credential refresh, or
        # configuration reload asks the parked server to reconnect. Waiting
        # across the old self-probe interval must not touch transport.
        state["backend_up"] = True
        parked_transport_calls = state["transport_calls"]
        await _real_sleep(0.16)
        assert parked_transport_calls == 2
        assert state["transport_calls"] == 2, (
            "parked server attempted reconnect without an explicit lifecycle request"
        )
        assert task.session is None

        # The production explicit-reconnect API still wakes the retained task
        # and restores its tools.
        assert reconnect_mcp_server("srv") is True
        for _ in range(200):
            await _real_sleep(0.01)
            if task.session is not None:
                break
        assert task.session is not None, (
            "parked server did not revive after an explicit reconnect "
            f"(transport_calls={state['transport_calls']})"
        )
        assert state["revived_registration"] >= 1, (
            "revived server did not re-register its tools"
        )

        task._shutdown_event.set()
        assert reconnect_mcp_server("srv") is True
        try:
            await asyncio.wait_for(run_task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()
        finally:
            mcp_tool._servers.pop(task.name, None)

    asyncio.run(_scenario())
