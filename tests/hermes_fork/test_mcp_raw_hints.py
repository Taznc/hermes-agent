"""Malformed MCP tool annotations fail safe before SDK validation (``hermes_fork.mcp_raw_hints``,
T2 anchors ``mcp-raw-annotations`` at both ``ClientSession`` constructions in
``tools/mcp_tool_transport.py``).

mcp 2.0.0 validates ``tools/list`` in pydantic lax mode: ``readOnlyHint: "yes"`` (or ``"true"``,
``1``, ``1.0``) parses as ``True`` so an untrusted server's tool skips the approval gate, and an
uncoercible value (``""``, ``2``, ``[1]``, ``{}``, a non-string ``title``, a non-object
``annotations``) fails the WHOLE list so the server never connects.

The integration tests run a hand-rolled server that answers with RAW JSON (no server-side SDK
model can normalise the values first) over stdio and Streamable HTTP, and connect through Hermes'
real discovery. Nothing in the MCP client is patched. Unit tests parse with the real ``mcp.types``
models and the SDK's own per-era wire validator.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

mcp_types = pytest.importorskip("mcp.types")
pytest.importorskip("mcp.shared.jsonrpc_dispatcher")  # the dispatcher seam is mcp >= 2.0
from mcp_types import methods as wire_methods  # noqa: E402

from hermes_fork.mcp_raw_hints import client_session, sanitize_tools_list  # noqa: E402

_SERVER = "rawhints"
_ABSENT = object()

# tool name -> raw ``annotations`` value on the wire (_ABSENT = no annotations key).
_VALID = {
    "ok_true": {"readOnlyHint": True},
    "ok_false": {"readOnlyHint": False},
    "ok_absent": _ABSENT,
    "ok_null": {"readOnlyHint": None},
}
_MALFORMED_HINT = {  # every one must read write-capable
    "bad_str_yes": {"readOnlyHint": "yes"},
    "bad_str_true": {"readOnlyHint": "true"},
    "bad_int_1": {"readOnlyHint": 1},
    "bad_float_1": {"readOnlyHint": 1.0},
    "bad_str_empty": {"readOnlyHint": ""},
    "bad_int_2": {"readOnlyHint": 2},
    "bad_list": {"readOnlyHint": [1]},
    "bad_obj": {"readOnlyHint": {}},
}
_MALFORMED_OTHER = {  # malformed fields other than readOnlyHint, and a non-object annotations
    "other_title": {"readOnlyHint": True, "title": 5},
    "other_destructive": {"readOnlyHint": True, "destructiveHint": "x"},
    "other_idempotent": {"idempotentHint": 2},
    "other_open_world": {"openWorldHint": [1]},
    "ann_str": "read-only",
    "ann_list": [],
    "ann_int": 3,
}
_MIXED = {**_VALID, **_MALFORMED_HINT, **_MALFORMED_OTHER}
# The only tools that may skip the approval gate: an honest ``true`` (a malformed sibling field
# does not demote a valid readOnlyHint; it is only made parseable).
_READ_ONLY = {"ok_true", "other_title", "other_destructive"}


def _wire_tools(spec: dict) -> list:
    tools = []
    for name, annotations in spec.items():
        tool = {"name": name, "description": name, "inputSchema": {"type": "object"}}
        if annotations is not _ABSENT:
            tool["annotations"] = copy.deepcopy(annotations)
        tools.append(tool)
    return tools


# ---------------------------------------------------------------------------- unit: raw rewrite


def _parse(raw: dict) -> dict:
    """What the 2.x client does after the dispatcher returns: wire validation, then mcp.types."""
    wire_methods.validate_server_result("tools/list", "2025-11-25", raw)
    return {t.name: t for t in mcp_types.ListToolsResult.model_validate(raw, by_name=False).tools}


def test_unsanitized_wire_values_are_the_two_sdk_defects():
    """The premise, pinned on the real SDK: coercion fails open, one bad value fails the list."""
    coerced = mcp_types.ListToolsResult.model_validate({"tools": _wire_tools({"t": {"readOnlyHint": "yes"}})})
    assert coerced.tools[0].annotations.read_only_hint is True
    with pytest.raises(ValidationError):
        mcp_types.ListToolsResult.model_validate({"tools": _wire_tools({**_VALID, "bad": {"readOnlyHint": 2}})})


def test_mixed_list_parses_whole_and_malformed_hints_read_write_capable():
    raw = {"tools": _wire_tools(_MIXED)}
    changes = sanitize_tools_list(raw)

    tools = _parse(raw)
    assert set(tools) == set(_MIXED)
    for name, tool in tools.items():
        hint = tool.annotations.read_only_hint if tool.annotations is not None else None
        assert (hint is True) is (name in _READ_ONLY), (name, tool.annotations)
    changed = {(name, field) for name, field, _ in changes}
    assert changed == {(n, "readOnlyHint") for n in _MALFORMED_HINT} | {
        ("other_title", "title"), ("other_destructive", "destructiveHint"),
        ("other_idempotent", "idempotentHint"), ("other_open_world", "openWorldHint"),
        ("ann_str", "annotations"), ("ann_list", "annotations"), ("ann_int", "annotations")}


def test_valid_annotations_are_left_byte_identical():
    raw = {"tools": _wire_tools({**_VALID, "full": {
        "title": "T", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
        "openWorldHint": False, "x-vendor": [1]}, "ann_null": None})}
    before = copy.deepcopy(raw)
    assert sanitize_tools_list(raw) == []
    assert raw == before


def test_other_malformed_fields_become_the_conservative_value():
    raw = {"tools": _wire_tools(_MALFORMED_OTHER)}
    sanitize_tools_list(raw)
    tools = _parse(raw)
    assert tools["other_title"].annotations.title is None
    assert tools["other_destructive"].annotations.destructive_hint is True
    assert tools["other_idempotent"].annotations.idempotent_hint is False
    assert tools["other_open_world"].annotations.open_world_hint is True
    for name in ("ann_str", "ann_list", "ann_int"):
        assert tools[name].annotations is None


def test_modern_wire_era_accepts_the_rewritten_list():
    raw = {"tools": _wire_tools(_MIXED), "cacheScope": "private", "resultType": "complete", "ttlMs": 0}
    with pytest.raises(ValidationError):
        wire_methods.validate_server_result("tools/list", "2026-07-28", copy.deepcopy(raw))
    sanitize_tools_list(raw)
    wire_methods.validate_server_result("tools/list", "2026-07-28", raw)


@pytest.mark.parametrize("raw", [None, [], "x", {}, {"tools": None}, {"tools": "x"}, {"tools": [1, None, "t"]}])
def test_shapes_outside_annotations_are_left_to_the_sdk(raw):
    before = copy.deepcopy(raw)
    assert sanitize_tools_list(raw) == []
    assert raw == before


def test_session_without_dispatcher_kwarg_is_built_exactly_as_upstream():
    """mcp 1.x / a test double: plain ``cls(read, write, **kwargs)``, no dispatcher."""
    calls = []

    class LegacySession:
        def __init__(self, read_stream, write_stream, message_handler=None):
            calls.append((read_stream, write_stream, message_handler))

    client_session(LegacySession, "r", "w", "srv", message_handler="h")
    assert calls == [("r", "w", "h")]


# ---------------------------------------------------------------------------- integration

# Hand-rolled MCP server answering with RAW JSON. ``tools/list`` re-reads the state file on every
# call so a test can change the list after connect. ``ping`` is answered -32601 (it is OPTIONAL),
# which sends the keepalive down its ``list_tools`` fallback.
_SERVER_SCRIPT = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

state_path, mode = sys.argv[1], sys.argv[2]


def answer(msg):
    if "id" not in msg:
        return None
    if msg.get("method") == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "raw", "version": "0"}}
    elif msg.get("method") == "tools/list":
        with open(state_path, encoding="utf-8") as fh:
            result = {"tools": json.load(fh)}
    else:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


if mode == "stdio":
    for line in sys.stdin:
        reply = answer(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    sys.exit(0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload=b""):
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        reply = answer(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
        self._send(202) if reply is None else self._send(200, json.dumps(reply).encode())

    def do_GET(self):
        self._send(405)

    do_DELETE = do_GET


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[3], "w", encoding="utf-8") as fh:
    fh.write(str(server.server_address[1]))
server.serve_forever()
'''


class _RawServer:
    def __init__(self, tmp_path: Path, mode: str, tools: dict):
        self.state = tmp_path / "tools.json"
        self.set_tools(tools)
        self.script = tmp_path / "raw_server.py"
        self.script.write_text(_SERVER_SCRIPT, encoding="utf-8")
        self.proc = None
        if mode == "stdio":
            self.config = {"command": sys.executable, "args": [str(self.script), str(self.state), "stdio"]}
            return
        port_file = tmp_path / "port"
        self.proc = subprocess.Popen([sys.executable, str(self.script), str(self.state), "http", str(port_file)])
        deadline = time.monotonic() + 30
        while not (port_file.exists() and port_file.read_text(encoding="utf-8")):
            if time.monotonic() > deadline or self.proc.poll() is not None:
                raise RuntimeError("fixture MCP server did not start")
            time.sleep(0.05)
        self.config = {"url": f"http://127.0.0.1:{port_file.read_text(encoding='utf-8')}/mcp"}

    def set_tools(self, tools: dict) -> None:
        self.state.write_text(json.dumps(_wire_tools(tools)), encoding="utf-8")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(10)


@pytest.fixture
def connect(tmp_path, monkeypatch):
    """Start the raw server in *mode* with *tools* and connect Hermes to it as ``trust: untrusted``.
    The keepalive is parked far out so the post-connect paths run only when a test drives them."""
    monkeypatch.setenv("NO_PROXY", "*")
    servers = []

    def _connect(mode: str, tools: dict):
        from tools.mcp_tool_discovery import register_mcp_servers
        raw = _RawServer(tmp_path, mode, tools)
        servers.append(raw)
        register_mcp_servers({_SERVER: {**raw.config, "trust": "untrusted", "connect_timeout": 20,
                                        "keepalive_interval": 3600}})
        return raw

    yield _connect
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    shutdown_mcp_servers()
    for raw in servers:
        raw.stop()


def _server_task():
    import tools.mcp_tool as mcp_core
    return next(s for s in mcp_core._servers.values() if s.name == _SERVER)


def _on_mcp_loop(factory):
    from tools.mcp_tool_loop import _run_on_mcp_loop
    return _run_on_mcp_loop(factory, timeout=20)


def _registered() -> set:
    from tools.registry import registry
    prefix = f"mcp__{_SERVER}__"
    return {e.name[len(prefix):] for e in registry.get_all_entries() if e.name.startswith(prefix)}


def _gate_skipped() -> set:
    """Tools the untrusted-server approval gate lets through without asking."""
    from tools.mcp_tool_handlers import _tool_is_read_only
    return {name for name in _registered() if _tool_is_read_only(_SERVER, name)}


@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_mixed_list_registers_every_tool_and_only_honest_true_skips_the_gate(connect, mode, caplog):
    caplog.set_level(logging.WARNING, logger="tools.mcp_tool")
    connect(mode, _MIXED)

    assert _registered() == set(_MIXED)
    assert _gate_skipped() == _READ_ONLY
    warned = [r.getMessage() for r in caplog.records if "malformed annotation" in r.getMessage()]
    assert any("'bad_str_yes'" in m and "readOnlyHint='yes'" in m and f"'{_SERVER}'" in m for m in warned)


@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_coercible_malformed_hint_no_longer_fails_open(connect, mode):
    """Defect A alone: every value here is coercible, so without the fix the list parses and
    'yes'/'true'/1/1.0 silently read as an honest ``readOnlyHint: true``."""
    coercible = ("bad_str_yes", "bad_str_true", "bad_int_1", "bad_float_1")
    connect(mode, {**_VALID, **{n: _MALFORMED_HINT[n] for n in coercible}})

    assert _registered() == set(_VALID) | set(coercible)
    assert _gate_skipped() == {"ok_true"}


@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_list_changed_refresh_after_connect_is_sanitized(connect, mode):
    """``_refresh_tools`` (the tools/list_changed path) re-lists through the same session."""
    raw = connect(mode, _VALID)
    assert _registered() == set(_VALID)

    raw.set_tools(_MIXED)
    _on_mcp_loop(lambda: _server_task()._refresh_tools())

    assert _registered() == set(_MIXED)
    assert _gate_skipped() == _READ_ONLY


def test_keepalive_list_tools_fallback_survives_a_malformed_list(connect, caplog):
    """ping is -32601 here, so the keepalive probe falls back to ``list_tools``; a malformed
    annotation appearing after connect must not fail it (a failure tears the session down)."""
    raw = connect("stdio", _VALID)
    server = _server_task()
    raw.set_tools(_MIXED)
    caplog.set_level(logging.WARNING, logger="tools.mcp_tool")

    for _ in range(3):
        _on_mcp_loop(server._keepalive_probe)

    assert server._ping_unsupported is True  # the list_tools fallback is what ran
    warned = [r.getMessage() for r in caplog.records if "malformed annotation" in r.getMessage()]
    assert len([m for m in warned if "'bad_int_2'" in m]) == 1  # once per session, not per poll
