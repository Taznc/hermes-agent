"""MCP App cards cross the gateway only as live completion projections."""

import json

import tui_gateway.server as server
from tools.mcp_tool_handlers import _McpAppToolResult


def test_mcp_app_card_is_projected_beside_ordinary_result(monkeypatch):
    sid = "mcp-app-live"
    events = []
    card = {"id": "x" * 20, "html": "<html><body>x</body></html>", "resourceUri": "ui://chart"}
    monkeypatch.setitem(server._sessions, sid, {
        "agent": None, "edit_snapshots": {}, "tool_started_at": {}, "tool_progress_mode": "all",
    })
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    server._on_tool_complete(sid, "call-1", "mcp__charts__chart", {}, _McpAppToolResult('{"result":"ordinary"}', card))

    payload = events[0][2]
    assert payload["result"] == {"result": "ordinary"}
    assert payload["mcp_app"] == card
    assert "mcpApp" not in payload["result"]
