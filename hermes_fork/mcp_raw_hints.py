"""Make malformed MCP tool annotations fail safe BEFORE the SDK validates ``tools/list`` (fork-owned, T1).

mcp 2.x validates the raw ``tools/list`` result with pydantic in lax mode, which causes two defects:

* fail-open: a coercible non-bool ``readOnlyHint`` (``"yes"``, ``"true"``, ``1``, ``1.0``) parses as
  ``True``. After parsing it looks exactly like an honest ``True``, so a tool on a ``trust: untrusted``
  server skips the approval gate (``hermes_fork.mcp_hints.read_only_hint`` only sees the parsed value);
* availability: an uncoercible value (``""``, ``2``, ``[1]``, ``{}``) in any annotation field, a
  non-string ``title``, or a non-object ``annotations`` raises ``ValidationError`` for the WHOLE list.
  The server never connects (parked after the retry budget) and a post-connect refresh fails.

The only place the raw JSON is still visible is the dispatcher, which ``ClientSession`` takes through
its public ``dispatcher=`` kwarg. ``client_session`` builds the session over a ``JSONRPCDispatcher``
subclass whose ``send_raw_request`` rewrites each ``tools/list`` page in place before it returns to
the SDK's validator. Discovery, ``_refresh_tools`` (list_changed), the keepalive ``list_tools``
fallback and the fork keepalive refresh all list through that one session, so all are covered.

Rewrites (valid ``true``/``false``/absent/``null`` are never touched; unknown keys are left to the
SDK, which ignores them):

* ``readOnlyHint``/``destructiveHint``/``idempotentHint``/``openWorldHint`` that is present, not
  ``null`` and not a JSON bool -> the conservative value (the spec defaults for a write-capable,
  destructive, non-idempotent, open-world tool: ``false``/``true``/``false``/``true``);
* ``title`` that is present, not ``null`` and not a string -> dropped;
* ``annotations`` that is present, not ``null`` and not an object -> dropped (no hints = write-capable).

Called from the two ``mcp-raw-annotations`` FORK ANCHORs in ``tools/mcp_tool_transport.py`` (the
only ``ClientSession`` constructions in the MCP stack). Upstream candidate; no upstream hook exists.
"""

from __future__ import annotations

import inspect
import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger("tools.mcp_tool")

# Wire key -> the value that makes the tool look least safe (MCP spec "Default" for each hint).
_CONSERVATIVE_BOOL_HINTS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


def _malformed(value: Any, expected: type) -> bool:
    # JSON yields only bool/int/float/str/list/dict/None; ``type is`` keeps 1/0 out of bool.
    return value is not None and type(value) is not expected


def sanitize_tools_list(raw: Any) -> list[tuple[Any, str, Any]]:
    """Rewrite malformed annotations in a raw ``tools/list`` result in place.

    Returns ``(tool name, field, original value)`` for every rewrite. Shapes the SDK rejects for
    reasons other than annotations (non-dict result, non-list ``tools``, non-dict tool) are left
    untouched so the SDK reports them itself.
    """
    tools = raw.get("tools") if isinstance(raw, dict) else None
    if not isinstance(tools, list):
        return []
    changes: list[tuple[Any, str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("annotations") is None:
            continue
        name, annotations = tool.get("name"), tool["annotations"]
        if not isinstance(annotations, dict):
            del tool["annotations"]
            changes.append((name, "annotations", annotations))
            continue
        for key, safe in _CONSERVATIVE_BOOL_HINTS.items():
            if _malformed(annotations.get(key), bool):
                changes.append((name, key, annotations[key]))
                annotations[key] = safe
        if _malformed(annotations.get("title"), str):
            changes.append((name, "title", annotations.pop("title")))
    return changes


@lru_cache(maxsize=1)
def _dispatcher_class():
    """The sanitizing ``JSONRPCDispatcher`` subclass, or ``None`` on an SDK without that seam (1.x)."""
    try:
        from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
    except ImportError:
        return None

    class AnnotationSanitizingDispatcher(JSONRPCDispatcher):
        def __init__(self, read_stream, write_stream, *, server_name: str = "", **kwargs):
            super().__init__(read_stream, write_stream, **kwargs)
            self.server_name = server_name
            self._reported: set = set()  # one WARNING per (tool, field) per session, not per poll

        async def send_raw_request(self, method, params, opts=None, **kwargs):
            raw = await super().send_raw_request(method, params, opts, **kwargs)
            if method == "tools/list":
                for name, field, value in sanitize_tools_list(raw):
                    if (name, field) in self._reported:
                        continue
                    self._reported.add((name, field))
                    logger.warning(
                        "MCP server '%s': tool %r sent malformed annotation %s=%r; treating it as "
                        "the conservative value (write-capable)", self.server_name, name, field, value)
            return raw

    return AnnotationSanitizingDispatcher


def _accepts_dispatcher(session_cls: Any) -> bool:
    try:
        return "dispatcher" in inspect.signature(session_cls).parameters
    except (TypeError, ValueError):
        return False


def client_session(session_cls: Any, read_stream: Any, write_stream: Any, server_name: str, **kwargs: Any):
    """``session_cls(read_stream, write_stream, **kwargs)``, but over the sanitizing dispatcher when
    the SDK takes ``dispatcher=``. Falls back to the plain construction otherwise (mcp 1.x, or a test
    double standing in for ``ClientSession``), which is exactly the upstream behaviour."""
    dispatcher_cls = _dispatcher_class()
    if dispatcher_cls is None or not _accepts_dispatcher(session_cls):
        return session_cls(read_stream, write_stream, **kwargs)
    dispatcher = dispatcher_cls(read_stream, write_stream, server_name=server_name)
    return session_cls(dispatcher=dispatcher, **kwargs)
