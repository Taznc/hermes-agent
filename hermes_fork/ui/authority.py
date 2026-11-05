"""Plugin UI bridge on the gateway session authority (``plugin-ui-authority`` anchor in
``gateway/local_desktop_ui.py::wire``).

Desktop chats now run their turns on the messaging gateway's session authority, not in the TUI
sidecar, so ``hermes_fork.ui.gateway`` (the sidecar half) owns no session for those agents and every
``ui.request`` returned None at once: ``ask`` never showed its card and fell back to clarify. Upstream
re-wired its own window bridges for that path (``local_desktop_ui``); this module does the same for
``plugin.request`` / ``plugin.event``, over the same server-request round trip clarify and
``theme.pick`` use (``gateway.session_server_requests.ask_viewer_result``). The renderer dispatches
``plugin.request`` by method name, so it needs no change.

:func:`bind` runs once per local-viewer turn and stores the turn's channel on the agent (the way
``wire`` installs its GUI callbacks); the agent's next turn replaces it, so there is no release step.
"""

from __future__ import annotations

from typing import Any

REQUEST_METHOD = "plugin.request"
EVENT_TYPE = "plugin.event"
_ATTR = "_fork_plugin_ui_route"


class _Route:
    """One local-viewer turn's channel to its session's attached viewers."""

    __slots__ = ("_agent", "_turn")

    def __init__(self, turn: Any, agent: Any) -> None:
        self._turn, self._agent = turn, agent

    def request(self, kind: str, payload: Any, timeout: float | None) -> dict | None:
        from gateway.session_server_requests import ask_viewer_result

        authority, session_id, _generation = self._turn._approval_owner
        live = authority.sessions.get(session_id)
        if live is None:
            return None
        params = {"kind": kind, "payload": payload}
        result = ask_viewer_result(live.event_stream, session_id, REQUEST_METHOD, params,
                                   timeout=timeout,  # type: ignore[arg-type]  # None = until answered
                                   cancelled=lambda: bool(getattr(self._agent, "_interrupt_requested", False)))
        return result if isinstance(result, dict) else None

    def emit(self, kind: str, payload: Any) -> bool:
        return bool(self._turn._publish_execution(EVENT_TYPE, {"kind": kind, "payload": payload}))


def bind(turn: Any, agent: Any) -> None:
    """Anchor entry point: route *agent*'s plugin UI calls through *turn* (a local-viewer turn)."""
    if agent is not None and getattr(turn, "_approval_owner", None) is not None:
        setattr(agent, _ATTR, _Route(turn, agent))


def route_for(agent: Any) -> _Route | None:
    """The bound channel for *agent*, else None (not a local-viewer turn on the authority)."""
    route = getattr(agent, _ATTR, None)
    return route if isinstance(route, _Route) else None
