"""Immutable views and bounded copies for the process-wide config caches.

Two hazards this module closes, both proven against the shared raw-config cache:

1. **Aliasing.** The readonly readers used to hand back the cache-owned dict. The copy
   boundary was only at the top level, so a caller that retained a nested list held the
   *cached* list, and mutating it corrupted the cache for every later reader.
2. **Unbounded copy.** ``copy._deepcopy_list`` is ``for a in x: append(deepcopy(a))`` and a
   list iterator re-reads the length every step, so deepcopying a list that grows at least
   as fast as it is copied NEVER returns — a live gateway spun a core inside that frame for
   16 hours while holding the global config lock.

The answer to (1) is a **frozen view**: built once per config-file signature, kept as the
cache's only owned config tree, and handed to every readonly caller. That keeps the readonly path
O(1), which is the whole reason it exists (it runs 2-3x per agent turn). The frozen containers are
``dict``/``list`` *subclasses* rather than ``MappingProxyType``/``tuple`` so the ~200 existing
readonly call sites that branch on ``isinstance(cfg, dict)`` keep working; only the mutating
methods are disabled. They also define ``__copy__``/``__deepcopy__`` so a caller that copies a
readonly result gets ordinary mutable containers back.

The answer to (2) is :func:`bounded_deepcopy`: it snapshots each list before iterating it (so a
concurrent appender can no longer extend the work in flight) and enforces node and wall-clock
ceilings, raising :class:`BoundedCopyBreach` naming the offending top-level key instead of
spinning forever.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "BoundedCopyBreach",
    "FrozenConfigError",
    "FrozenDict",
    "FrozenList",
    "bounded_deepcopy",
    "copy_budget_for",
    "freeze",
    "thaw",
]

# Ceilings for a single bounded copy. The node floor is generous next to a real config (a
# ~9 KB config.yaml is ~770 nodes) so only a pathological structure can reach it; the time
# ceiling is the backstop for a source that is being mutated while we copy it.
DEFAULT_COPY_NODE_FLOOR = 2_000_000
DEFAULT_COPY_TIME_BUDGET_S = 5.0
_TIME_CHECK_EVERY = 4096

_FROZEN_HINT = (
    "config snapshots are read-only — use load_config()/read_raw_config() for a mutable copy"
)


class FrozenConfigError(TypeError):
    """Raised when a caller mutates a readonly config snapshot.

    ``TypeError`` so it reads like any other "unsupported operation" and is caught by call
    sites that already guard mutations defensively.
    """


class BoundedCopyBreach(Exception):
    """A bounded copy crossed its node or elapsed ceiling.

    ``top_key`` names the top-level config key being copied when the ceiling was crossed —
    the one piece of information that makes the failure actionable.
    """

    def __init__(self, top_key: Any, nodes: int, elapsed: float, limit: str):
        super().__init__(
            f"config copy exceeded its {limit} ceiling at top-level key {top_key!r} "
            f"after {nodes} nodes / {elapsed:.2f}s")
        self.top_key, self.nodes, self.elapsed, self.limit = top_key, nodes, elapsed, limit


def _blocked(self: Any, *_args: Any, **_kwargs: Any):
    raise FrozenConfigError(_FROZEN_HINT)


class FrozenDict(dict):
    """A ``dict`` whose mutators raise. Nested values are frozen too."""

    __slots__ = ()

    __setitem__ = __delitem__ = _blocked
    clear = pop = popitem = setdefault = update = _blocked
    __ior__ = _blocked

    def __copy__(self) -> Dict[str, Any]:
        return thaw(self)

    def __deepcopy__(self, memo: Optional[dict] = None) -> Dict[str, Any]:
        return thaw(self)

    def __reduce__(self):  # pickling a snapshot yields a plain, mutable dict
        return (dict, (dict(self),))


class FrozenList(list):
    """A ``list`` whose mutators raise. Nested values are frozen too."""

    __slots__ = ()

    __setitem__ = __delitem__ = _blocked
    append = extend = insert = remove = pop = clear = sort = reverse = _blocked
    __iadd__ = __imul__ = _blocked

    def __copy__(self) -> list:
        return thaw(self)

    def __deepcopy__(self, memo: Optional[dict] = None) -> list:
        return thaw(self)

    def __reduce__(self):
        return (list, (list(self),))


def freeze(value: Any) -> Tuple[Any, int]:
    """Return ``(frozen_view, node_count)`` for ``value``.

    Containers become :class:`FrozenDict`/:class:`FrozenList`; anything else is returned
    as-is (config leaves are YAML scalars). The node count is what
    :func:`copy_budget_for` sizes a later bounded copy against.
    """
    nodes = 0

    def _walk(node: Any) -> Any:
        nonlocal nodes
        nodes += 1
        if isinstance(node, dict):
            return FrozenDict((key, _walk(item)) for key, item in node.items())
        if isinstance(node, tuple):  # already immutable; freeze the contents only
            return tuple(_walk(item) for item in node)
        if isinstance(node, list):
            return FrozenList(_walk(item) for item in node)
        return node

    return _walk(value), nodes


def thaw(value: Any) -> Any:
    """Return a plain, mutable deep copy of a frozen view.

    Terminating by construction: a frozen view cannot grow, which is what makes this a safe
    fail-closed target when a bounded copy of the live snapshot breaches its ceiling.
    """
    if isinstance(value, dict):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(thaw(item) for item in value)
    if isinstance(value, list):
        return [thaw(item) for item in value]
    return value


def copy_budget_for(known_nodes: int) -> int:
    """Node ceiling for copying a snapshot whose known-good size is ``known_nodes``.

    Sized off the structure we actually parsed (with generous headroom) so a breach means
    "this is no longer the structure we cached", not "the user's config is large".
    """
    return max(DEFAULT_COPY_NODE_FLOOR, known_nodes * 2 + 1024)


class _Budget:
    __slots__ = ("nodes", "node_limit", "deadline", "time_budget", "started", "top_key")

    def __init__(self, node_limit: int, time_budget_s: float):
        self.nodes = 0
        self.node_limit = node_limit
        self.time_budget = time_budget_s
        self.started = time.monotonic()
        self.deadline = self.started + time_budget_s
        self.top_key: Any = None

    def spend(self) -> None:
        self.nodes += 1
        if self.nodes > self.node_limit:
            raise BoundedCopyBreach(
                self.top_key, self.nodes, time.monotonic() - self.started, "node")
        if self.nodes % _TIME_CHECK_EVERY == 0 and time.monotonic() > self.deadline:
            raise BoundedCopyBreach(
                self.top_key, self.nodes, time.monotonic() - self.started, "elapsed")


def _copy_node(value: Any, budget: _Budget) -> Any:
    budget.spend()
    if isinstance(value, dict):
        # Snapshot the items first: iterating a mapping that is being mutated raises, and
        # copying from the snapshot keeps the work bounded by what we saw.
        return {key: _copy_node(item, budget) for key, item in list(value.items())}
    if isinstance(value, list):
        # list(value) is the load-bearing call: it fixes the length NOW, so an appender
        # racing this copy can no longer extend the work in flight (the wedge mechanism).
        return [_copy_node(item, budget) for item in list(value)]
    if isinstance(value, tuple):
        return tuple(_copy_node(item, budget) for item in value)
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    return copy.deepcopy(value)


def bounded_deepcopy(
    source: Any, *, node_limit: int = DEFAULT_COPY_NODE_FLOOR,
    time_budget_s: float = DEFAULT_COPY_TIME_BUDGET_S,
) -> Any:
    """Deep copy ``source`` under node and wall-clock ceilings.

    Raises :class:`BoundedCopyBreach` — naming the top-level key in flight — instead of
    spinning forever when the source is pathological or is being mutated concurrently.
    """
    budget = _Budget(node_limit, time_budget_s)
    if isinstance(source, dict):
        budget.spend()
        out: Dict[Any, Any] = {}
        for key, item in list(source.items()):
            budget.top_key = key
            out[key] = _copy_node(item, budget)
        return out
    return _copy_node(source, budget)
