"""``ctx.fork`` — the fork's additive plugin API (``plugin-context-fork`` anchor in ``hermes_cli/plugins.py``).

The anchor imports :data:`fork` into ``PluginContext``'s class body; it is a descriptor, so
``ctx.fork`` returns this context's :class:`ForkPluginContext`. Nothing on the stock ``PluginContext``
changes: plugins written for upstream keep working, and a plugin that uses ``ctx.fork`` can probe for it
with ``getattr(ctx, "fork", None)`` to stay loadable on upstream.

Every fork extension point hangs off this object (FORK.md "fork extension points"); X02 adds ``ui``
and ``register_tool(..., interactive=True)``.
"""

from __future__ import annotations

from typing import Any, Callable


class ForkPluginContext:
    """Fork capabilities for one plugin (one ``PluginContext``)."""

    __slots__ = ("_ctx", "_ui")

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._ui = None

    @property
    def ui(self):
        """:class:`~hermes_fork.ui.bridge.PluginUI` for this plugin, resolved against whichever agent
        turn is running when it is called (tool handlers get the same object as their ``ui`` kwarg)."""
        if self._ui is None:
            from hermes_fork.ui.bridge import PluginUI
            self._ui = PluginUI(self._ctx.plugin_id)
        return self._ui

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Callable, *,
                      interactive: bool = False, **kwargs: Any):
        """Stock ``ctx.register_tool`` (same arguments, same return) plus ``interactive``.

        ``interactive=True`` marks a tool that waits on a human: it is a batch barrier that never runs
        concurrently or under the sequential tool deadline (like ``clarify``), and its tool.start /
        tool.complete always reach the UI. The mark is dropped when the plugin unloads."""
        handle = self._ctx.register_tool(name, toolset, schema, handler, **kwargs)
        if interactive and handle is not None:
            from hermes_fork.ui.interactive import mark_interactive, unmark_interactive
            plugin_id = self._ctx.plugin_id
            mark_interactive(name, plugin_id)
            self._ctx.on_unload(lambda: unmark_interactive(name, plugin_id))
        return handle


class _ForkDescriptor:
    """``PluginContext.fork``: one :class:`ForkPluginContext` per plugin context, created on first use."""

    _attr = "_fork_ctx"

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"_fork_ctx_{name}"

    def __get__(self, ctx: Any, owner: type | None = None):
        if ctx is None:
            return self
        cached = ctx.__dict__.get(self._attr)
        if cached is None:
            cached = ctx.__dict__[self._attr] = ForkPluginContext(ctx)
        return cached


fork = _ForkDescriptor()
