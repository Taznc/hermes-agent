"""Tool-dispatch half of the plugin UI bridge (``plugin-ui-dispatch`` anchor in ``model_tools._execute_tool``).

For a tool registered by a plugin, the anchor merges :func:`plugin_tool_kwargs` UNDER upstream's
dispatch kwargs (upstream keys win — a setdefault), and ``registry.dispatch`` passes each one only if
the handler's signature accepts it:

- ``ui``: a :class:`~hermes_fork.ui.bridge.PluginUI` bound to this plugin and this turn's agent.
- ``clarify_callback``: the turn agent's clarify callback (the name open upstream PR #70626 threads
  to every tool). When that PR lands, upstream's own key wins and this entry is a no-op.

Built-in tools never get either: the owner lookup answers only for names a plugin registered.
"""

from __future__ import annotations

from typing import Any


def plugin_owner(name: str) -> str | None:
    """Plugin id that registered tool *name* in the active profile's plugin manager, else None."""
    try:
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
    except Exception:
        return None
    # Manager doubles (and a manager built before plugin discovery) may lack the set; the anchor
    # runs on every tool call, so a missing attribute must mean "not a plugin tool", never a raise.
    if name not in getattr(manager, "_plugin_tool_names", ()):
        return None
    for plugin_key, registrations in list(manager._ownership_ledger.items()):
        if any(r.kind == "tool" and r.key == name and r.active for r in registrations):
            return plugin_key
    for plugin_key, loaded in list(manager._plugins.items()):
        if name in (loaded.tools_registered or ()):
            return plugin_key
    return None


def plugin_tool_kwargs(name: str) -> dict[str, Any]:
    """Anchor entry point: ``{}`` for built-in tools, else ``ui`` (+ ``clarify_callback`` when set)."""
    plugin_id = plugin_owner(name)
    if plugin_id is None:
        return {}
    from agent.subagent_lifecycle import get_active_subagent_parent
    from hermes_fork.ui.bridge import PluginUI

    agent = get_active_subagent_parent()
    kwargs: dict[str, Any] = {"ui": PluginUI(plugin_id, agent)}
    clarify = getattr(agent, "clarify_callback", None)
    if callable(clarify):
        kwargs["clarify_callback"] = clarify
    return kwargs
