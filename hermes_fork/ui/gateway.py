"""Gateway half of the plugin UI bridge (``plugin-ui-bridge`` anchor in ``tui_gateway/agent_callbacks.py``
+ the existing ``gateway-fork-methods`` anchor in ``tui_gateway/server.py``).

The anchor hands :func:`bridge_callbacks` two senders written as literals in ``agent_callbacks.py``
(``server_requests.send("plugin.request", ...)`` / ``_emit("plugin.event", ...)``) — upstream's wire
catalog test finds senders by scanning ``tui_gateway/*.py`` for those literals, so they cannot live here.
:func:`install` runs from ``register_fork_gateway_methods`` once ``server.py`` has registered every
upstream module: it declares the two contracts and rebinds ``server._tool_lifecycle_required_for_ui``
so an interactive plugin tool always emits tool.start/complete (the desktop card hangs off that call).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

RequestSender = Callable[[Any, str, dict, "float | None"], "dict | None"]
EmitSender = Callable[[Callable, str, dict], bool]

_request_sender: RequestSender | None = None
_emit_sender: EmitSender | None = None
_server: Any = None  # the tui_gateway.server module that ran install()


def bridge_callbacks(request: RequestSender, emit: EmitSender):
    """Anchor entry point: keep the two literal senders; return ``(ui_request_callback, ui_emit_callback)``."""
    global _request_sender, _emit_sender
    _request_sender, _emit_sender = request, emit
    return ui_request_callback, ui_emit_callback


def ui_request_callback(sid: str, kind: str, payload: Any, timeout: float | None) -> dict | None:
    """Send ``plugin.request`` to *sid*'s UI and block (``timeout`` None = until answered/cancelled).
    ``None`` on timeout, cancel, error response or a non-dict answer."""
    if _request_sender is None:
        return None
    from tui_gateway import server_requests
    result = _request_sender(server_requests, sid, {"kind": kind, "payload": payload}, timeout)
    return result if isinstance(result, dict) else None


def ui_emit_callback(sid: str, kind: str, payload: Any) -> bool:
    server = _server
    if _emit_sender is None or server is None:
        return False
    return bool(_emit_sender(server._emit, sid, {"kind": kind, "payload": payload}))


# PluginUI calls these through ``sys.modules`` (see hermes_fork.ui.bridge._gateway).
request = ui_request_callback
emit = ui_emit_callback


def session_for_agent(agent: Any) -> str | None:
    """The live UI session whose turn *agent* is running, else None.

    Only a session whose agent IS the bound agent qualifies: a delegated child, a background/btw side
    agent or a closed session never gets to raise a card on a chat window. The turn's
    ``HERMES_UI_SESSION_ID`` (bound by the prompt turn, carried to tool worker threads by
    ``propagate_context_to_thread``) is the fast path; otherwise the live sessions are scanned."""
    server = _server
    if server is None or agent is None:
        return None
    sessions = server._sessions
    from gateway.session_context import get_session_env
    sid = get_session_env("HERMES_UI_SESSION_ID")
    if sid and (sessions.get(sid) or {}).get("agent") is agent:
        return sid
    for sid, session in list(sessions.items()):
        if isinstance(session, dict) and session.get("agent") is agent:
            return sid
    return None


_LIFECYCLE_FN = "_tool_lifecycle_required_for_ui"


def _wrap_lifecycle(original: Callable[[str], bool]) -> Callable[[str], bool]:
    if getattr(original, "__fork_wrapped__", None) is not None:
        return original
    from hermes_fork.ui.interactive import is_interactive_tool

    def _tool_lifecycle_required_for_ui(name: str) -> bool:
        return bool(original(name)) or is_interactive_tool(name)

    _tool_lifecycle_required_for_ui.__fork_wrapped__ = original
    _tool_lifecycle_required_for_ui.__doc__ = original.__doc__
    return _tool_lifecycle_required_for_ui


def install(server: Any) -> None:
    """Declare the wire contracts and hook the lifecycle predicate on the real gateway module."""
    global _server
    from hermes_fork.ui.contracts import declare_contracts
    from tui_gateway.contracts import registry as contracts

    original = getattr(server, _LIFECYCLE_FN, None)
    if not callable(original) or not hasattr(server, "_sessions"):
        # Upstream renamed/moved the predicate (tests/hermes_fork/ui pins it) or this is not the gateway.
        if contracts.STRICT and original is None and hasattr(server, "_sessions"):
            raise RuntimeError(f"tui_gateway.server.{_LIFECYCLE_FN} is gone; update hermes_fork.ui.gateway")
        logger.warning("plugin UI bridge not installed on %r: no %s", server, _LIFECYCLE_FN)
        return
    declare_contracts()
    setattr(server, _LIFECYCLE_FN, _wrap_lifecycle(original))
    _server = server
