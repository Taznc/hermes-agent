"""Behavior contracts for the bounded phase-1 MCP Apps resource host."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _tool(name="chart", resource_uri="ui://charts/summary"):
    return SimpleNamespace(name=name, description="chart", annotations=None, meta={"ui": {"resourceUri": resource_uri}})


def _result(*, error=False, text="ordinary result"):
    return SimpleNamespace(isError=error, content=[SimpleNamespace(type="text", text=text)], structuredContent=None, meta=None)


def _resource(*, text="<html><body>chart</body></html>", mime="text/html"):
    return SimpleNamespace(contents=[SimpleNamespace(text=text, mimeType=mime)])


def test_discovery_records_only_bounded_ui_resource_uri():
    from tools import mcp_tool
    from tools.mcp_tool_registration import _record_tool_ui_metadata

    mcp_tool._mcp_tool_ui_resources.clear()
    _record_tool_ui_metadata("charts", [_tool()])
    assert mcp_tool._mcp_tool_ui_resources["charts"] == {"chart": "ui://charts/summary"}

    _record_tool_ui_metadata("invalid", [_tool("bad", "https://example.test/app")])
    assert "invalid" not in mcp_tool._mcp_tool_ui_resources


def test_successful_tool_call_keeps_card_transient_without_replacing_text(monkeypatch):
    from tools import mcp_tool
    from tools import mcp_tool_handlers as handlers

    server = SimpleNamespace(session=SimpleNamespace(), _rpc_lock=None, _pending_call_context=None)
    server.session.call_tool = AsyncMock(return_value=_result())
    server.session.read_resource = AsyncMock(return_value=_resource())
    mcp_tool._mcp_tool_ui_resources["charts"] = {"chart": "ui://charts/summary"}

    def run(coro_or_factory, timeout=30):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        loop = asyncio.new_event_loop()
        try:
            server._rpc_lock = asyncio.Lock()
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with patch.dict(mcp_tool._servers, {"charts": server}), patch(
        "tools.mcp_tool_loop._run_on_mcp_loop", side_effect=run
    ):
        result = handlers._make_tool_handler("charts", "chart", 10)({})

    payload = json.loads(result)
    assert payload["result"] == "ordinary result"
    assert "mcpApp" not in payload
    card = getattr(result, "mcp_app_card")
    assert {key: value for key, value in card.items() if key != "id"} == {
        "serverId": "charts",
        "toolName": "chart",
        "resourceUri": "ui://charts/summary",
        "html": "<html><body>chart</body></html>",
    }
    assert len(card["id"]) >= 20
    server.session.read_resource.assert_awaited_once_with("ui://charts/summary")


def test_bad_resources_and_failed_calls_keep_ordinary_result_without_card(monkeypatch):
    from tools import mcp_tool
    from tools import mcp_tool_handlers as handlers

    server = SimpleNamespace(session=SimpleNamespace(), _rpc_lock=None, _pending_call_context=None)
    server.session.call_tool = AsyncMock(return_value=_result())
    server.session.read_resource = AsyncMock(return_value=_resource(mime="text/plain"))
    mcp_tool._mcp_tool_ui_resources["charts"] = {"chart": "ui://charts/summary"}

    def run(coro_or_factory, timeout=30):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        loop = asyncio.new_event_loop()
        try:
            server._rpc_lock = asyncio.Lock()
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with patch.dict(mcp_tool._servers, {"charts": server}), patch(
        "tools.mcp_tool_loop._run_on_mcp_loop", side_effect=run
    ):
        payload = json.loads(handlers._make_tool_handler("charts", "chart", 10)({}))

    assert payload == {"result": "ordinary result"}

    server.session.call_tool = AsyncMock(return_value=_result(error=True, text="failed"))
    server.session.read_resource.reset_mock()
    with patch.dict(mcp_tool._servers, {"charts": server}), patch(
        "tools.mcp_tool_loop._run_on_mcp_loop", side_effect=run
    ):
        failed = json.loads(handlers._make_tool_handler("charts", "chart", 10)({}))
    assert failed["error"] == "failed"
    server.session.read_resource.assert_not_awaited()


def test_legacy_embedded_resource_rendering_is_unchanged():
    from tools.mcp_tool_handlers import _render_call_tool_result

    embedded = SimpleNamespace(
        type="resource",
        resource=SimpleNamespace(uri="note://x", mimeType="text/plain", text="legacy text"),
    )
    payload = json.loads(_render_call_tool_result(SimpleNamespace(
        isError=False, content=[embedded], structuredContent=None, meta=None
    ), "charts"))

    assert "legacy text" in payload["result"]
    assert "mcpApp" not in payload
