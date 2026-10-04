"""Refresh an MCP server's tool list on the periodic keepalive (fork-owned, T1).

A stateless Streamable-HTTP server keeps no session, so it can never push
``notifications/tools/list_changed``; tools it gains after connect (a user linking an account)
stayed invisible until a restart. The keepalive loop already wakes every ``keepalive_interval``
(default 180 s), so after each successful keepalive this re-reads ``tools/list`` and, only when
it differs from the last list, runs the upstream ``_refresh_tools`` path (refresh lock, stale-name
removal, re-registration): the same registry update a ``list_changed`` notification gets, so an
already-running agent picks the new tools up at its next turn through the between-turns refresh.

An unchanged list touches nothing: no re-registration, no schema-cache write, no INFO log.
Called from the ``mcp-keepalive-tool-refresh`` FORK ANCHOR in
``tools/mcp_tool_server_run.py::MCPServerRunMixin._wait_for_lifecycle_event``.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("tools.mcp_tool")

# Same bound the keepalive probe puts on its own RPCs: a hung list never wedges the check task.
_LIST_TIMEOUT = 30.0


def _tool_fingerprint(tools) -> str:
    """Order-insensitive identity of a ``tools/list`` result: names AND contract (schema,
    description, annotations), so a same-named tool whose schema moved also refreshes."""
    def _dump(tool):
        dump = getattr(tool, "model_dump", None)
        if callable(dump):
            try:
                return dump(mode="json", by_alias=True, exclude_none=True)
            except Exception:  # noqa: BLE001
                pass
        return {"name": getattr(tool, "name", None), "repr": repr(tool)}

    items = sorted((json.dumps(_dump(t), sort_keys=True, default=str) for t in tools or []))
    return "\n".join(items)


async def _check_and_refresh(server) -> None:
    from tools.mcp_tool_common import _core

    async with server._rpc_lock:
        session = server.session
        if session is None:
            return  # torn down since the keepalive; reconnect discovery re-lists anyway
        listed = await asyncio.wait_for(
            _core._paginate_full_list(session.list_tools, "tools", server.name), timeout=_LIST_TIMEOUT)
    if _tool_fingerprint(listed) == _tool_fingerprint(server._tools):
        return
    logger.debug("MCP server '%s': tools/list changed since the last listing; refreshing tools", server.name)
    await server._refresh_tools()  # logs the added/removed names itself


def _polls_tool_list(server) -> bool:
    """The same gate discovery and the keepalive use (``_advertises_tools``): skip only servers
    whose captured capabilities omit ``tools`` (prompt-/resource-only, tools/list would -32601);
    no capability info is the legacy fallback and is polled, since its tools were discovered.
    ``tools.listChanged`` is deliberately NOT a reason to skip: the TypeScript SDK's ``McpServer``
    advertises it unconditionally, including on stateless transports that have no channel to
    deliver it (the Our House gateway: sdk 1.29, ``sessionIdGenerator: undefined``)."""
    return server._advertises_tools()


def schedule_keepalive_tool_refresh(server) -> None:
    """After a successful keepalive: re-list the server's tools in a background task (tracked in
    ``_pending_refresh_tasks``, so shutdown cancels it) and refresh the registry if they changed.
    Skipped for servers without the ``tools`` capability and while a refresh is pending."""
    try:
        if server.session is None or not _polls_tool_list(server) or server._pending_refresh_tasks:
            return

        async def _run():
            try:
                await _check_and_refresh(server)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — health is the keepalive's job, not this check's
                logger.debug("MCP server '%s': keepalive tool-list check failed: %s", server.name, exc)

        task = asyncio.create_task(_run())
        server._pending_refresh_tasks.add(task)
        task.add_done_callback(server._pending_refresh_tasks.discard)
    except Exception:  # noqa: BLE001 — never break the keepalive loop
        logger.debug("MCP server '%s': keepalive tool refresh not scheduled",
                     getattr(server, "name", "?"), exc_info=True)
