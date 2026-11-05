"""``tools.tool_search.eager``: tools that tool search never defers (``tool-search-eager`` anchor in
``tools/tool_search.py::is_deferrable_tool_name``).

Upstream's ``tools.tool_search.defer`` can only ADD names to the deferred set; every plugin and MCP tool
is deferrable with no way out. That buried the fork's ``ask`` plugin tool behind
``tool_search``/``tool_describe``/``tool_call`` while core ``clarify`` stayed ambient, so models kept
calling clarify (upstream's own A/B, noted beside ``_DEFAULT_DEFERRED_TOOLS``, measured the same
effect: deferring clarify collapsed structured-question use 18/18 -> 7/18). Listing a name here keeps
it in the model-visible array on every surface::

    tools:
      tool_search:
        eager: [ask]

The anchor runs once per tool per assembly, so the set is cached per profile config file and re-read
only when that file changes.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, tuple[int, frozenset[str]]] = {}  # config path -> (mtime_ns, eager names)


def parse(raw: Any) -> frozenset[str]:
    """Eager names from a ``tools.tool_search`` mapping; a malformed value is logged and ignored."""
    value = raw.get("eager") if isinstance(raw, dict) else None
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple, set)):
        logger.warning("tools.tool_search.eager is %r, expected a YAML list of tool names "
                       "(e.g. [ask]) - ignoring it.", value)
        return frozenset()
    return frozenset(str(n).strip() for n in value if str(n).strip())


def _load() -> frozenset[str]:
    import hermes_cli.config as cfg

    tools_cfg = (cfg.load_config_readonly() or {}).get("tools")
    return parse(tools_cfg.get("tool_search") if isinstance(tools_cfg, dict) else None)


def eager_tools() -> frozenset[str]:
    """The active profile's eager set (empty on any failure: a config problem must not break assembly)."""
    try:
        from hermes_constants import get_hermes_home

        path = str(get_hermes_home() / "config.yaml")
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = -1
        with _lock:
            hit = _cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        names = _load()
        with _lock:
            _cache[path] = (mtime, names)
        return names
    except Exception as exc:  # health: allow BLE001 -- optional pin; assembly must not fail on it
        logger.debug("tools.tool_search.eager unavailable: %s", exc)
        return frozenset()


def is_eager(name: str) -> bool:
    """Anchor entry point: True when *name* must stay in the model-visible tool array."""
    return name in eager_tools()


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
