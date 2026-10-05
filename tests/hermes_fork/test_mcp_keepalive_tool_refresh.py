"""The periodic MCP keepalive re-reads ``tools/list`` (``hermes_fork.mcp_keepalive_refresh``).

A stateless Streamable-HTTP server has no session, so it can never push
``notifications/tools/list_changed``. The ``mcp-keepalive-tool-refresh`` FORK ANCHOR in
``MCPServerRunMixin._wait_for_lifecycle_event`` is what is under test: after each successful
keepalive the tool list is compared with the registered one and, only when it differs, the
upstream ``_refresh_tools`` path updates the registry.

Every test runs a real ``mcp.server.MCPServer`` (``stateless_http=True``) in a subprocess and
connects to it through Hermes' real discovery. Nothing in the MCP client is patched; the only
override is the keepalive floor, so an interval of a fraction of a second is honoured.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

_mcp_server_mod = pytest.importorskip("mcp.server")
if not hasattr(_mcp_server_mod, "MCPServer"):
    pytest.skip("fixture server needs mcp >= 2.0 (MCPServer)", allow_module_level=True)

_SERVER = "house"
_KEEPALIVE = 0.4
_GOOGLE = ("gmail_search", "gmail_send", "calendar_list")

# Stateless Streamable-HTTP MCP server. ``link_gmail`` grows the tool list the way the Our House
# gateway does when a user links Google; nothing is pushed to the client. With ``noping`` the
# server answers ``ping`` with -32601 (ping is OPTIONAL). Every request's method is logged.
_SERVER_SCRIPT = r'''
import asyncio, json, socket, sys
import uvicorn
from mcp.server import MCPServer

log_path, port_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
server = MCPServer("house")
GOOGLE = ("gmail_search", "gmail_send", "calendar_list")

def lights_on(room: str) -> str:
    return f"lights on in {room}"

def google_tool(name):
    def tool(query: str = "") -> str:
        return f"{name}:{query}"
    return tool

def link_gmail() -> str:
    for name in GOOGLE:
        server.add_tool(google_tool(name), name=name, description=f"Google tool {name}")
    return "linked"

if not mode.startswith("empty-"):
    server.add_tool(lights_on)
    server.add_tool(link_gmail)

def wrapped(app):
    async def asgi(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/add":
            server.add_tool(lights_on)
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"added"})
            return
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await app(scope, receive, send)
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        try:
            msg = json.loads(body)
        except Exception:
            msg = {}
        method = msg.get("method") if isinstance(msg, dict) else None
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(method) + "\n")
        if mode.endswith("noping") and method == "ping":
            payload = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                  "error": {"code": -32601, "message": "Method not found"}}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        sent = False
        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await app(scope, replay, send)
    return asgi

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
app = wrapped(server.streamable_http_app(stateless_http=True, json_response=True))
uv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

async def main():
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    while not uv.started:
        await asyncio.sleep(0.01)
    open(port_path, "w", encoding="utf-8").write(str(sock.getsockname()[1]))
    await task

asyncio.run(main())
'''


class _House:
    def __init__(self, tmp_path: Path, mode: str):
        self.log = tmp_path / f"{mode}.log"
        port_file = tmp_path / f"{mode}.port"
        script = tmp_path / "house_server.py"
        script.write_text(_SERVER_SCRIPT, encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(script), str(self.log), str(port_file), mode])
        deadline = time.monotonic() + 30
        while not (port_file.exists() and port_file.read_text(encoding="utf-8")):
            if time.monotonic() > deadline or self.proc.poll() is not None:
                raise RuntimeError("fixture MCP server did not start")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port_file.read_text(encoding='utf-8')}/mcp"

    def methods(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)


@pytest.fixture
def connect(tmp_path, monkeypatch):
    """Start the fixture server in *mode* and connect Hermes to it with a short keepalive."""
    import tools.mcp_tool as mcp_core

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(mcp_core, "_MIN_KEEPALIVE_INTERVAL", 0.1)  # floor only; the loop is real
    houses = []

    def _connect(mode: str, *, empty: bool = False) -> _House:
        from tools.mcp_tool_discovery import register_mcp_servers
        house = _House(tmp_path, mode)
        houses.append(house)
        register_mcp_servers({_SERVER: {"url": house.url, "keepalive_interval": _KEEPALIVE}})
        if empty:
            assert _tool("lights_on") not in _registered() and _tool("link_gmail") not in _registered()
        else:
            assert _registered() >= {_tool("lights_on"), _tool("link_gmail")}
        return house

    yield _connect
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    shutdown_mcp_servers()
    for house in houses:
        house.stop()


def _tool(name: str) -> str:
    return f"mcp__{_SERVER}__{name}"


def _registered() -> set:
    from tools.registry import registry
    return {e.name for e in registry.get_all_entries() if e.name.startswith(f"mcp__{_SERVER}__")}


def _dispatch(name: str, args: dict):
    from tools.registry import registry
    return json.loads(registry.dispatch(_tool(name), args))


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _keepalives(house: _House, method: str) -> int:
    return house.methods().count(method)


@pytest.mark.parametrize("mode", ["ping", "noping"])
def test_grown_tool_list_reaches_the_registry_within_one_keepalive(connect, mode):
    house = connect(mode)
    assert _dispatch("link_gmail", {}).get("result") == "linked"  # server-side growth, nothing pushed
    start = time.monotonic()

    google = {_tool(n) for n in _GOOGLE}
    assert _wait_for(lambda: google <= _registered(), timeout=_KEEPALIVE * 3 + 5), (
        f"{mode}: new tools never registered; server saw {house.methods()}")
    assert time.monotonic() - start < _KEEPALIVE * 3 + 5
    # A NEW session builds its tools from the registry. MCP tools are deferred behind tool_search
    # by default, so they are in the catalog the bridge searches, found by it, and callable.
    from model_tools import get_tool_definitions, handle_function_call
    catalog = {t["function"]["name"] for t in get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)}
    assert google <= catalog
    found = json.loads(handle_function_call("tool_search", {"queries": ["gmail search"]}))
    assert _tool("gmail_search") in json.dumps(found)
    assert _dispatch("gmail_search", {"query": "rent"}).get("result") == "gmail_search:rent"
    if mode == "ping":
        assert "ping" in house.methods()
    else:
        assert "ping" in house.methods() and house.methods().count("ping") == 1  # latched after -32601


@pytest.mark.parametrize("mode", ["ping", "noping"])
def test_unchanged_tool_list_causes_no_registry_churn_or_log_spam(connect, mode, caplog):
    from tools.registry import registry
    house = connect(mode)
    # Let the first keepalive settle (the -32601 ping latch logs once per connection, by design).
    listed = _keepalives(house, "tools/list")
    assert _wait_for(lambda: _keepalives(house, "tools/list") > listed, timeout=_KEEPALIVE * 2 + 5)
    before = (registry._generation, _registered())
    listed = _keepalives(house, "tools/list")
    caplog.set_level(logging.DEBUG, logger="tools.mcp_tool")

    # Several keepalive cycles, each of which re-reads tools/list.
    assert _wait_for(lambda: _keepalives(house, "tools/list") >= listed + 3, timeout=_KEEPALIVE * 4 + 5)

    assert (registry._generation, _registered()) == before
    loud = [r.getMessage() for r in caplog.records
            if r.levelno >= logging.INFO and r.name == "tools.mcp_tool"]
    assert loud == [], loud


@pytest.mark.parametrize("tool_search", ["off", "auto"])
def test_running_session_sees_new_tool_at_its_next_turn(connect, monkeypatch, tool_search):
    """Same contract as a ``tools/list_changed`` refresh: the registry changes at once and an
    already-built agent's tool array is never touched mid-turn. At its next turn start the
    between-turns refresh runs: with tool_search off the new tools append at the tail (existing
    slots unchanged, cached prefix intact); with the default deferral the array stays
    byte-identical and the bridge's live catalog already serves the new tools."""
    import model_tools
    import tools.tool_search as ts
    from agent.turn_context import _refresh_mcp_tools_between_turns

    cfg = ts.ToolSearchConfig.from_raw({"enabled": tool_search})
    monkeypatch.setattr(ts, "load_config", lambda: cfg)
    model_tools._clear_tool_defs_cache()
    connect("ping")
    built = list(model_tools.get_tool_definitions(quiet_mode=True))
    agent = SimpleNamespace(tools=list(built), valid_tool_names={t["function"]["name"] for t in built},
                            enabled_toolsets=None, disabled_toolsets=None)
    _dispatch("link_gmail", {})
    google = {_tool(n) for n in _GOOGLE}
    assert _wait_for(lambda: google <= _registered(), timeout=_KEEPALIVE * 3 + 5)

    assert agent.tools == built  # nothing touches a live agent's array mid-turn

    _refresh_mcp_tools_between_turns(agent)  # what run_conversation does at turn start

    names = [t["function"]["name"] for t in agent.tools]
    assert names[:len(built)] == [t["function"]["name"] for t in built]
    if tool_search == "off":
        assert set(names[len(built):]) == google
        assert google <= agent.valid_tool_names
    else:
        assert agent.tools == built
        found = json.loads(model_tools.handle_function_call("tool_search", {"queries": ["calendar list"]}))
        assert _tool("calendar_list") in json.dumps(found)


@pytest.mark.parametrize("mode", ["ping", "noping"])
def test_server_without_captured_capabilities_is_still_polled(connect, mode):
    """No capability info is ``_advertises_tools``' legacy fallback: a server whose tools were
    discovered and whose keepalive treats it as tool-capable must be polled too."""
    import tools.mcp_tool as mcp_core
    house = connect(mode)
    server = next(s for s in mcp_core._servers.values() if s.name == _SERVER)
    server.initialize_result = None  # a session that captured no capabilities
    assert _dispatch("link_gmail", {}).get("result") == "linked"

    google = {_tool(n) for n in _GOOGLE}
    assert _wait_for(lambda: google <= _registered(), timeout=_KEEPALIVE * 3 + 5), (
        f"{mode}: new tools never registered; server saw {house.methods()}")


@pytest.mark.parametrize("mode", ["ping", "noping"])
@pytest.mark.parametrize("legacy", [False, True])
def test_initially_empty_tool_list_grows_after_keepalive(connect, mode, legacy):
    """A completed empty discovery is not evidence that tools/list is unsupported."""
    import tools.mcp_tool as mcp_core

    house = connect(f"empty-{mode}", empty=True)
    server = next(s for s in mcp_core._servers.values() if s.name == _SERVER)
    assert server._ready.is_set() and server._ever_connected and not server._tools
    if legacy:
        server.initialize_result = None  # no capability info, as on legacy servers
    before = _keepalives(house, "tools/list")
    with urllib.request.urlopen(house.url.replace("/mcp", "/add"), timeout=3) as response:
        assert response.read() == b"added"
    assert _wait_for(lambda: _tool("lights_on") in _registered(), timeout=_KEEPALIVE * 3 + 5), (
        f"{mode}/{legacy}: first tool never registered; server saw {house.methods()}")
    assert _keepalives(house, "tools/list") > before
    assert "ping" in house.methods()
    assert _dispatch("lights_on", {"room": "kitchen"}).get("result") == "lights on in kitchen"


@pytest.mark.parametrize("tools_cap, polls", [
    (SimpleNamespace(list_changed=False), True),
    # Advertised but undeliverable on a stateless transport: still poll.
    (SimpleNamespace(list_changed=True), True),
    (None, False),
])
def test_servers_advertising_tools_are_polled(tools_cap, polls):
    from hermes_fork.mcp_keepalive_refresh import _polls_tool_list
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("house")
    # No captured capabilities (_advertises_tools' legacy fallback): a bare task stays
    # ping-only, but discovery must poll even when the first list was empty.
    assert _polls_tool_list(server) is False
    server._ever_connected = True
    assert _polls_tool_list(server) is True
    server._tools = [SimpleNamespace(name="lights_on")]
    assert _polls_tool_list(server) is True
    server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(tools=tools_cap))
    assert _polls_tool_list(server) is polls
