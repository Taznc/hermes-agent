"""Wire contracts of the plugin UI bridge, declared into upstream's contract registry at gateway
registration (never at import), so ``scripts/gen_gateway_contracts.py`` keeps rendering upstream's
committed TS/OpenRPC byte-for-byte (F24 option A); its ``gateway-contracts-upstream-only`` anchor
filters these names back out through :func:`hermes_fork.gateway.registry.upstream_only`.

Desktop (X02b) hand-writes the matching types in ``apps/desktop/src/fork/``.
"""

from __future__ import annotations

from tui_gateway.contracts import registry as _contracts
from tui_gateway.contracts.base import JsonValue, Payload, Result
from tui_gateway.contracts.server_requests import ServerRequestParams

PLUGIN_REQUEST = "plugin.request"
PLUGIN_EVENT = "plugin.event"


class PluginRequestParams(ServerRequestParams):
    """A plugin tool asks the UI. ``kind`` is ``<plugin_id>/<name>``; ``payload`` is the plugin's own JSON."""

    kind: str
    payload: JsonValue = None


class PluginRequestResult(Result):
    """``{payload}`` = the contributor's answer; ``{unsupported: true}`` = no UI contributor for ``kind``
    (answered promptly, so the plugin falls back instead of waiting out the timeout)."""

    payload: JsonValue = None
    unsupported: bool | None = None


class PluginEventPayload(Payload):
    """Fire-and-forget notification from a plugin tool; no answer expected."""

    kind: str
    payload: JsonValue = None


def declare_contracts() -> tuple[str, ...]:
    """Declare ``plugin.request`` / ``plugin.event`` once per process (re-registration is a no-op)."""
    from hermes_fork.gateway.registry import mark_fork_contract

    if PLUGIN_REQUEST not in _contracts.SERVER_REQUESTS:
        _contracts.server_request(
            PLUGIN_REQUEST, params=PluginRequestParams, result=PluginRequestResult,
            doc="Fork (X02): a plugin tool asks the UI for input; the desktop renders it inline under the tool call.")
    if PLUGIN_EVENT not in _contracts.EVENTS:
        _contracts.event(PLUGIN_EVENT, PluginEventPayload,
                         doc="Fork (X02): fire-and-forget notification from a plugin tool.")
    mark_fork_contract(PLUGIN_REQUEST, PLUGIN_EVENT)
    return PLUGIN_REQUEST, PLUGIN_EVENT
