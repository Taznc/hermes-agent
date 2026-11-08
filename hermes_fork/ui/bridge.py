"""``PluginUI`` — the object a plugin tool handler receives as its ``ui`` kwarg (and ``ctx.fork.ui``).

Import-light on purpose (stdlib only at module level): it is constructed on the tool-dispatch path in
every surface, CLI and messaging gateways included, where no UI bridge exists and every request
simply returns ``None``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_UNBOUND = object()


def _default_timeout() -> float | None:
    """``agent.clarify_timeout`` (the single wait budget for every human prompt); ``<= 0`` = unlimited."""
    try:
        from tools.clarify_gateway import get_clarify_timeout
        timeout = get_clarify_timeout()
    except Exception:
        return 300.0
    return float(timeout) if timeout and timeout > 0 else None


def _resolve_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return _default_timeout()
    return float(timeout) if timeout > 0 else None


def _gateway():
    """The gateway half, only when a TUI/desktop gateway runs in this process (never imported here:
    importing it would drag the pydantic wire contracts into the CLI)."""
    return sys.modules.get("hermes_fork.ui.gateway")


class PluginUI:
    """UI requests/events for one plugin, bound to one agent turn.

    ``request`` blocks until the attached UI answers and returns the result dict — ``{"payload": ...}``
    from the plugin's UI contributor, or ``{"unsupported": True}`` when the UI has no contributor for
    ``kind`` — or ``None`` when there is no interactive UI for this turn (CLI, messaging gateway, cron,
    subagent), on timeout, and on cancel (interrupt / session close). ``None`` and ``unsupported`` both
    mean "fall back" (e.g. to ``clarify_callback``).

    ``kind`` must be ``<plugin_id>/<name>``; anything else raises ``ValueError`` before any wire traffic
    (a tool handler's exception becomes the tool's error result).
    """

    __slots__ = ("plugin_id", "_agent")

    def __init__(self, plugin_id: str, agent: Any = _UNBOUND) -> None:
        self.plugin_id = str(plugin_id)
        self._agent = agent

    def __repr__(self) -> str:
        return f"PluginUI(plugin_id={self.plugin_id!r})"

    # -- validation ----------------------------------------------------------------------------

    def _check(self, kind: Any, payload: Any) -> tuple[str, Any]:
        prefix = f"{self.plugin_id}/"
        if not isinstance(kind, str) or not kind.startswith(prefix) or len(kind) == len(prefix):
            raise ValueError(f"plugin UI kind {kind!r} must be namespaced '{prefix}<name>'")
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"plugin UI payload for {kind!r} is not JSON-serialisable: {exc}") from None
        return kind, payload

    def _bound_agent(self):
        agent = self._agent
        if agent is _UNBOUND:
            from agent.subagent_lifecycle import get_active_subagent_parent
            agent = get_active_subagent_parent()
        return agent

    def _channel(self):
        """``(gateway module, session id)`` for this turn's live UI, else ``(None, None)``."""
        gateway = _gateway()
        if gateway is None:
            return None, None
        sid = gateway.session_for_agent(self._bound_agent())
        return (gateway, sid) if sid else (None, None)

    def _route(self):
        """``(request, emit)`` for this turn's attached viewers: the TUI sidecar's session, else the
        gateway session authority's local-viewer turn (``hermes_fork.ui.authority``); None when neither."""
        gateway, sid = self._channel()
        if gateway is not None:
            return (lambda kind, payload, timeout: gateway.request(sid, kind, payload, timeout),
                    lambda kind, payload: gateway.emit(sid, kind, payload))
        authority = sys.modules.get("hermes_fork.ui.authority")
        route = authority.route_for(self._bound_agent()) if authority is not None else None
        owner = getattr(self._bound_agent(), "_fork_human_input_owner", None)
        if owner is not None and getattr(owner, "messaging", False):
            return owner.request, lambda *_: False
        return (route.request, route.emit) if route is not None else None

    # -- public API ----------------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether an interactive UI is attached to this turn (a request may still be ``unsupported``)."""
        return self._route() is not None

    def request(self, kind: str, payload: Any = None, timeout: float | None = None) -> dict | None:
        """Ask the UI and block for the answer. ``timeout`` None → ``agent.clarify_timeout``; ``<= 0`` →
        wait until answered or cancelled."""
        kind, payload = self._check(kind, payload)
        route = self._route()
        if route is None:
            return None
        from hermes_fork.human_input_bridge.routing import route_ask
        return route_ask(self, kind, payload, _resolve_timeout(timeout), route[0])

    def emit(self, kind: str, payload: Any = None) -> bool:
        """Fire-and-forget ``plugin.event``; True when a frame was written to the UI."""
        kind, payload = self._check(kind, payload)
        route = self._route()
        if route is None:
            return False
        return route[1](kind, payload)
