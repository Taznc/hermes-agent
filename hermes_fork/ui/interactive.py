"""Interactive plugin tools (``interactive-plugin-tools`` anchor in ``agent/tool_dispatch_helpers.py``).

An interactive tool waits on a human, so it must be a batch barrier exactly like ``clarify``: never
run concurrently with other calls, and never under the sequential tool deadline (it owns its wait via
``agent.clarify_timeout``). Upstream expresses both through membership in ``_NEVER_PARALLEL_TOOLS``,
a frozenset that ``agent/tool_executor.py`` imports BY NAME at import time, so the anchor rebinds it
to :class:`NeverParallelTools` — a frozenset whose ``in`` also answers for tools registered with
``ctx.fork.register_tool(..., interactive=True)`` at any later time.

Standard library only: this module is imported while ``agent`` is still initialising.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
# tool name -> plugin id that registered it interactive
_INTERACTIVE: dict[str, str] = {}


def mark_interactive(name: str, plugin_id: str) -> None:
    with _lock:
        _INTERACTIVE[name] = plugin_id


def unmark_interactive(name: str, plugin_id: str | None = None) -> None:
    """Drop *name* (only if *plugin_id* still owns the mark, when given)."""
    with _lock:
        if plugin_id is None or _INTERACTIVE.get(name) == plugin_id:
            _INTERACTIVE.pop(name, None)


def is_interactive_tool(name: object) -> bool:
    return isinstance(name, str) and name in _INTERACTIVE


def interactive_tools() -> frozenset[str]:
    with _lock:
        return frozenset(_INTERACTIVE)


class NeverParallelTools(frozenset):
    """Upstream's frozenset, plus every tool currently marked interactive (live, not a snapshot).

    Only ``in`` is widened — that is the one operation upstream performs on the set. Iteration, ``len``
    and set algebra still see upstream's members only, so nothing that enumerates the set changes."""

    def __contains__(self, name: object) -> bool:
        return frozenset.__contains__(self, name) or is_interactive_tool(name)

    def __reduce__(self):
        return (NeverParallelTools, (frozenset(self),))


def with_interactive_plugin_tools(never_parallel: frozenset) -> NeverParallelTools:
    """Anchor entry point: wrap upstream's ``_NEVER_PARALLEL_TOOLS`` (idempotent)."""
    if isinstance(never_parallel, NeverParallelTools):
        return never_parallel
    return NeverParallelTools(never_parallel)
