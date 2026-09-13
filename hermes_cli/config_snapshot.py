"""Read-only views and bounded copies for the process-wide config caches.

Two hazards this module closes, both proven against the shared raw-config cache:

1. **Aliasing.** The readonly readers used to hand back the cache-owned dict. The copy
   boundary was only at the top level, so a caller that retained a nested list held the
   *cached* list, and mutating it corrupted the cache for every later reader.
2. **Unbounded copy.** ``copy._deepcopy_list`` is ``for a in x: append(deepcopy(a))`` and a
   list iterator re-reads the length every step, so deepcopying a list that grows at least
   as fast as it is copied NEVER returns — a live gateway spun a core inside that frame for
   16 hours while holding the global config lock.

The answer to (1) is :func:`readonly_view`. The cache keeps the parsed tree **private** and
never publishes it; a readonly caller gets a *fresh* :class:`FrozenDict` wrapper whose nested
containers are wrapped lazily, one level per access. Three properties follow, and each is a
regression test:

* **Nothing published is identical to anything cached**, at any depth — every container a
  caller can reach is a wrapper minted for that access.
* **Nothing published is mutable.** The wrappers subclass ``dict``/``list`` so the ~200
  existing readonly call sites that branch on ``isinstance(cfg, dict)`` keep working, but they
  are *hollow*: the real data lives in a slot and the built-in storage stays empty. That is
  what makes them immune to ``dict.__setitem__(view, ...)`` / ``list.append(view, ...)`` —
  unbound base-class calls bypass any Python-level override, so a subclass that merely
  overrides its mutators is not immutable. Such a call writes into the throwaway wrapper's
  empty storage and is invisible to every reader.
* **Building a view is O(1).** The readonly path exists because it runs 2-3x per agent turn;
  it must not silently reintroduce a per-call deep copy.

The cost of hollow storage is that C-level fast paths which read a dict's/list's built-in
storage directly — ``json.dumps`` and unbound base-class accessors such as
``dict.get(view, key)`` — see an empty container. ``yaml`` is handled below by registering
representers; for anything else, ``copy.deepcopy(view)`` (or the mutable reader) yields
ordinary containers, which is the documented escape hatch.

The answer to (2) is :func:`bounded_deepcopy`: it snapshots each list before iterating it (so
a concurrent appender can no longer extend the work in flight) and enforces node and
wall-clock ceilings, raising :class:`BoundedCopyBreach` naming the offending top-level key
instead of spinning forever. Both it and :func:`thaw` memoise on ``id()`` like
``copy.deepcopy``, so YAML anchors/aliases keep their shared identity and a self-referential
document copies instead of recursing forever.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

__all__ = [
    "BoundedCopyBreach",
    "FrozenConfigError",
    "FrozenDict",
    "FrozenList",
    "bounded_deepcopy",
    "copy_budget_for",
    "count_nodes",
    "readonly_view",
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

_SCALARS = (str, bytes, int, float, bool, type(None))


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


def _unwrap(value: Any) -> Any:
    """The plain object behind a view (or ``value`` itself), for comparisons and arithmetic."""
    if isinstance(value, (FrozenDict, FrozenList)):
        return value._src
    return value


class FrozenDict(dict):
    """An immutable, lazily-wrapping view of a mapping.

    Hollow on purpose: ``dict`` is only the base class so ``isinstance(view, dict)`` holds,
    and the built-in storage is left empty so no mutation — Python-level or unbound
    base-class — can reach the mapping this view reads from.
    """

    __slots__ = ("_src",)

    def __init__(self, src: Dict[Any, Any]):  # noqa: D107 - see class docstring
        self._src = src

    # -- reads -------------------------------------------------------------------------
    def __getitem__(self, key: Any) -> Any:
        return readonly_view(self._src[key])

    def get(self, key: Any, default: Any = None) -> Any:
        if key in self._src:
            return readonly_view(self._src[key])
        return default

    def __iter__(self) -> Iterator[Any]:
        return iter(self._src)

    def __len__(self) -> int:
        return len(self._src)

    def __contains__(self, key: Any) -> bool:
        return key in self._src

    def keys(self):
        return self._src.keys()

    def values(self) -> Any:
        return tuple(readonly_view(item) for item in self._src.values())

    def items(self) -> Any:
        return tuple((key, readonly_view(item)) for key, item in self._src.items())

    def __eq__(self, other: Any) -> bool:
        return self._src == _unwrap(other)

    def __ne__(self, other: Any) -> bool:
        return self._src != _unwrap(other)

    def __or__(self, other: Any) -> Dict[Any, Any]:
        return {**self._src, **_unwrap(other)}

    def __ror__(self, other: Any) -> Dict[Any, Any]:
        return {**_unwrap(other), **self._src}

    def __repr__(self) -> str:
        return repr(self._src)

    # -- copies ------------------------------------------------------------------------
    def copy(self) -> Dict[str, Any]:
        """A plain, mutable deep copy — a shallow copy would leak frozen values."""
        return thaw(self)

    __copy__ = copy

    def __deepcopy__(self, memo: Optional[dict] = None) -> Dict[str, Any]:
        return thaw(self)

    def __reduce__(self):  # pickling a snapshot yields a plain, mutable dict
        return (dict, (thaw(self),))

    # -- mutation ----------------------------------------------------------------------
    __setitem__ = __delitem__ = _blocked
    clear = pop = popitem = setdefault = update = _blocked
    __ior__ = _blocked


class FrozenList(list):
    """An immutable, lazily-wrapping view of a sequence. Hollow — see :class:`FrozenDict`."""

    __slots__ = ("_src",)

    def __init__(self, src: List[Any]):  # noqa: D107 - see class docstring
        self._src = src

    # -- reads -------------------------------------------------------------------------
    def __getitem__(self, index: Any) -> Any:
        return readonly_view(self._src[index])

    def __iter__(self) -> Iterator[Any]:
        return (readonly_view(item) for item in list(self._src))

    def __reversed__(self) -> Iterator[Any]:
        return (readonly_view(item) for item in reversed(list(self._src)))

    def __len__(self) -> int:
        return len(self._src)

    def __contains__(self, item: Any) -> bool:
        return _unwrap(item) in self._src

    def index(self, *args: Any) -> int:
        return self._src.index(*args)

    def count(self, item: Any) -> int:
        return self._src.count(_unwrap(item))

    def __eq__(self, other: Any) -> bool:
        return self._src == _unwrap(other)

    def __ne__(self, other: Any) -> bool:
        return self._src != _unwrap(other)

    def __lt__(self, other: Any) -> bool:
        return self._src < _unwrap(other)

    def __le__(self, other: Any) -> bool:
        return self._src <= _unwrap(other)

    def __gt__(self, other: Any) -> bool:
        return self._src > _unwrap(other)

    def __ge__(self, other: Any) -> bool:
        return self._src >= _unwrap(other)

    def __add__(self, other: Any) -> List[Any]:
        return self._src + _unwrap(other)

    def __radd__(self, other: Any) -> List[Any]:
        return _unwrap(other) + self._src

    def __mul__(self, count: Any) -> List[Any]:
        return self._src * count

    __rmul__ = __mul__

    def __repr__(self) -> str:
        return repr(self._src)

    # -- copies ------------------------------------------------------------------------
    def copy(self) -> List[Any]:
        """A plain, mutable deep copy — a shallow copy would leak frozen values."""
        return thaw(self)

    __copy__ = copy

    def __deepcopy__(self, memo: Optional[dict] = None) -> List[Any]:
        return thaw(self)

    def __reduce__(self):
        return (list, (thaw(self),))

    # -- mutation ----------------------------------------------------------------------
    __setitem__ = __delitem__ = _blocked
    append = extend = insert = remove = pop = clear = sort = reverse = _blocked
    __iadd__ = __imul__ = _blocked


def readonly_view(value: Any) -> Any:
    """Wrap ``value`` in an immutable view, one level deep.

    Containers become a **fresh** :class:`FrozenDict`/:class:`FrozenList` on every call, so
    nothing a caller holds is ever the object the cache holds; nested containers are wrapped
    on access, which keeps this O(1). Scalars — every config leaf — are immutable already and
    are returned as they are.
    """
    if isinstance(value, (FrozenDict, FrozenList)) or isinstance(value, _SCALARS):
        return value
    if isinstance(value, dict):
        return FrozenDict(value)
    if isinstance(value, list):
        return FrozenList(value)
    if isinstance(value, tuple):
        # Immutable itself, but its items may not be. Tuples do not occur in parsed YAML;
        # this only covers hand-built defaults, so eager wrapping costs nothing in practice.
        return tuple(readonly_view(item) for item in value)
    return value


def copy_budget_for(known_nodes: int) -> int:
    """Node ceiling for copying a snapshot whose known-good size is ``known_nodes``.

    Sized off the structure we actually parsed (with generous headroom) so a breach means
    "this is no longer the structure we cached", not "the user's config is large".
    """
    return max(DEFAULT_COPY_NODE_FLOOR, known_nodes * 2 + 1024)


def count_nodes(value: Any) -> int:
    """Nodes in ``value``, counting each shared/aliased object once.

    Memoised on ``id()`` so a self-referential YAML document terminates here too.
    """
    seen: Dict[int, Any] = {}
    total = 0
    stack = [value]
    while stack:
        node = stack.pop()
        total += 1
        if isinstance(node, _SCALARS):
            continue
        key = id(node)
        if key in seen:
            continue
        seen[key] = node
        if isinstance(node, dict):
            stack.extend(list(node.values()))
        elif isinstance(node, (list, tuple)):
            stack.extend(list(node))
    return total


class _Budget:
    """Node and wall-clock ceilings for one copy. ``None`` for either disables it."""

    __slots__ = ("nodes", "node_limit", "deadline", "started", "top_key")

    def __init__(self, node_limit: Optional[int], time_budget_s: Optional[float]):
        self.nodes = 0
        self.node_limit = node_limit
        self.started = time.monotonic()
        self.deadline = None if time_budget_s is None else self.started + time_budget_s
        self.top_key: Any = None

    def spend(self) -> None:
        self.nodes += 1
        if self.node_limit is not None and self.nodes > self.node_limit:
            raise BoundedCopyBreach(
                self.top_key, self.nodes, time.monotonic() - self.started, "node")
        if (self.deadline is not None and self.nodes % _TIME_CHECK_EVERY == 0
                and time.monotonic() > self.deadline):
            raise BoundedCopyBreach(
                self.top_key, self.nodes, time.monotonic() - self.started, "elapsed")


def _copy_node(value: Any, budget: _Budget, memo: Dict[int, Any], keep: List[Any]) -> Any:
    budget.spend()
    if isinstance(value, _SCALARS):
        return value
    key = id(value)
    if key in memo:
        # A YAML anchor referenced more than once, or a cycle. Reusing the copy preserves
        # the source's sharing exactly as copy.deepcopy does, and terminates on cycles.
        return memo[key]
    if isinstance(value, dict):
        out_dict: Dict[Any, Any] = {}
        memo[key] = out_dict
        keep.append(value)  # id() is only unique while the original is alive
        for item_key, item in list(value.items()):
            out_dict[item_key] = _copy_node(item, budget, memo, keep)
        return out_dict
    if isinstance(value, list):
        # list(value) is the load-bearing call: it fixes the length NOW, so an appender
        # racing this copy can no longer extend the work in flight (the wedge mechanism).
        out_list: List[Any] = []
        memo[key] = out_list
        keep.append(value)
        for item in list(value):
            out_list.append(_copy_node(item, budget, memo, keep))
        return out_list
    if isinstance(value, tuple):
        out_tuple = tuple(_copy_node(item, budget, memo, keep) for item in value)
        memo[key] = out_tuple
        keep.append(value)
        return out_tuple
    copied = copy.deepcopy(value)
    memo[key] = copied
    keep.append(value)
    return copied


def _copy_tree(source: Any, budget: _Budget) -> Any:
    memo: Dict[int, Any] = {}
    keep: List[Any] = []
    if isinstance(source, dict) and not isinstance(source, FrozenDict):
        budget.spend()
        out: Dict[Any, Any] = {}
        memo[id(source)] = out
        keep.append(source)
        for key, item in list(source.items()):
            budget.top_key = key
            out[key] = _copy_node(item, budget, memo, keep)
        return out
    if isinstance(source, FrozenDict):
        # Named top-level keys matter for the breach message here too.
        budget.spend()
        out = {}
        memo[id(source)] = out
        keep.append(source)
        for key, item in list(source._src.items()):
            budget.top_key = key
            out[key] = _copy_node(item, budget, memo, keep)
        return out
    if isinstance(source, FrozenList):
        return _copy_node(source._src, budget, memo, keep)
    return _copy_node(source, budget, memo, keep)


def bounded_deepcopy(
    source: Any, *, node_limit: Optional[int] = DEFAULT_COPY_NODE_FLOOR,
    time_budget_s: Optional[float] = DEFAULT_COPY_TIME_BUDGET_S,
) -> Any:
    """Deep copy ``source`` into plain mutable containers, under node and time ceilings.

    Raises :class:`BoundedCopyBreach` — naming the top-level key in flight — instead of
    spinning forever when the source is pathological or is being mutated concurrently.
    """
    return _copy_tree(source, _Budget(node_limit, time_budget_s))


def thaw(value: Any) -> Any:
    """Plain, mutable deep copy of ``value`` with no ceilings.

    Terminating by construction — every list is snapshotted before it is walked and every
    object is memoised — which is what makes it a safe fail-closed target when a bounded
    copy of the live snapshot breaches its ceiling.
    """
    return _copy_tree(value, _Budget(None, None))


def _register_yaml_representers() -> None:
    """Let ``yaml.dump``/``yaml.safe_dump`` serialize a readonly view.

    The views are hollow, so yaml's ``dict``/``list`` representers would emit an empty
    document. Representing them explicitly keeps ``yaml.safe_dump(load_config_readonly())``
    behaving as it did when the readonly readers returned plain containers.
    """
    import yaml

    def _dict(dumper, data):
        return dumper.represent_dict(data.items())

    def _list(dumper, data):
        return dumper.represent_list(list(data))

    for dumper_cls in (yaml.SafeDumper, yaml.Dumper):
        dumper_cls.add_representer(FrozenDict, _dict)
        dumper_cls.add_representer(FrozenList, _list)


_register_yaml_representers()
