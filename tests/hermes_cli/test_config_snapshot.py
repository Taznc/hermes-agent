"""Unit tests for hermes_cli/config_snapshot.py — read-only views and bounded copies.

These are the primitives behind the config-cache fix; their integration with the real config
readers is covered by tests/hermes_cli/test_config_cache_aliasing.py.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest
import yaml

from hermes_cli.config_snapshot import (
    BoundedCopyBreach, FrozenConfigError, bounded_deepcopy, copy_budget_for, count_nodes,
    readonly_view, thaw)


def test_views_keep_isinstance_contracts():
    """~200 readonly call sites branch on isinstance(cfg, dict) — that must keep working."""
    src = {"a": [1, {"b": "c"}], "d": "e"}
    view = readonly_view(src)
    assert isinstance(view, dict) and isinstance(view["a"], list)
    assert isinstance(view["a"][1], dict)
    assert view == src
    assert list(view.keys()) == ["a", "d"]
    assert view.get("missing") is None
    assert view.get("d") == "e"
    assert len(view) == 2 and "a" in view
    assert dict(view.items()) == {"a": [1, {"b": "c"}], "d": "e"}
    assert [x for x in view["a"]] == [1, {"b": "c"}]
    assert count_nodes(src) == 6  # root, list, 1, inner dict, "c", "e"


def test_a_view_never_exposes_the_source_object():
    """The whole point: nothing reachable through a view is an object the source holds."""
    src = {"outer": {"items": [1, 2]}}
    view = readonly_view(src)
    assert view is not src
    assert view["outer"] is not src["outer"]
    assert view["outer"]["items"] is not src["outer"]["items"]
    # Two accesses do not even share a wrapper, so nothing is retained across callers.
    assert view["outer"] is not view["outer"]


def test_base_class_mutators_cannot_reach_the_source():
    """Unbound ``dict``/``list`` methods bypass any Python-level override — the reason the
    views are hollow rather than mutator-overriding subclasses.

    Each call either writes into the throwaway wrapper's own (empty) storage or raises
    because that storage is empty; both are fine. What must hold is that the source is
    untouched, which a mutator-overriding subclass of ``dict``/``list`` cannot guarantee.
    """
    src = {"outer": {"items": [1, 2]}}
    view = readonly_view(src)

    for mutate in (lambda: dict.__setitem__(view, "injected", 1),
                   lambda: dict.__setitem__(view["outer"], "injected", 1),
                   lambda: dict.update(view["outer"], {"injected": 1}),
                   lambda: list.append(view["outer"]["items"], 99),
                   lambda: list.__setitem__(view["outer"]["items"], 0, 99),
                   lambda: list.extend(view["outer"]["items"], [99])):
        try:
            mutate()
        except (IndexError, KeyError):
            pass  # empty wrapper storage — also unreachable

    assert src == {"outer": {"items": [1, 2]}}
    assert readonly_view(src) == {"outer": {"items": [1, 2]}}


def test_every_container_mutator_is_refused():
    view = readonly_view({"a": [1, 2], "b": {"c": 3}})
    for mutate in (lambda: view.__setitem__("x", 1),
                   lambda: view.pop("a"),
                   lambda: view.update({"x": 1}),
                   lambda: view.clear(),
                   lambda: view["a"].append(3),
                   lambda: view["a"].sort(),
                   lambda: view["a"].__setitem__(0, 9),
                   lambda: view["b"].setdefault("d", 4)):
        with pytest.raises(FrozenConfigError):
            mutate()
    assert view == {"a": [1, 2], "b": {"c": 3}}


def test_building_a_view_is_constant_time():
    """The readonly path runs 2-3x per agent turn; it must not deep copy per call."""
    small = {"k": [1]}
    big = {"bulk": [{"k": i, "v": [i, i + 1]} for i in range(50_000)], "k": [1]}

    def _per_call(src):
        readonly_view(src)["k"]
        started = time.perf_counter()
        for _ in range(2000):
            readonly_view(src)["k"]
        return (time.perf_counter() - started) / 2000

    # A per-call copy would scale with the tree; a view does not.
    assert _per_call(big) < _per_call(small) * 5 + 5e-5


def test_a_view_is_yaml_serializable():
    """Callers dump config; the views are hollow, so yaml needs the registered representers."""
    src = {"nested": {"items": [1, 2]}, "flag": True}
    view = readonly_view(src)
    assert yaml.safe_load(yaml.safe_dump(view)) == src
    assert yaml.safe_load(yaml.dump(view)) == src


def test_thaw_and_deepcopy_return_plain_mutable_containers():
    view = readonly_view({"a": [1, {"b": []}]})
    for plain in (thaw(view), copy.deepcopy(view), view.copy()):
        assert type(plain) is dict and type(plain["a"]) is list
        assert type(plain["a"][1]) is dict
        plain["a"].append(2)
        plain["a"][1]["b"].append(3)
    assert view == {"a": [1, {"b": []}]}


def test_copy_budget_scales_with_the_known_snapshot():
    """A budget must never be tighter than the structure we already cached."""
    assert copy_budget_for(0) >= 1
    huge = copy_budget_for(10_000_000)
    assert huge > 10_000_000, "budget must exceed the snapshot it was sized against"


def test_bounded_copy_of_a_live_growing_list_terminates():
    """The exact pathological shape, off any cache: a list growing while it is copied."""
    growing = list(range(1000))
    source = {"growing": growing}
    stop = threading.Event()

    def _appender():
        while not stop.is_set():
            for _ in range(1000):
                growing.append(0)

    thread = threading.Thread(target=_appender, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        out = bounded_deepcopy(source, node_limit=500_000, time_budget_s=5.0)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(timeout=10)
    assert elapsed < 20, f"bounded copy took {elapsed:.1f}s"
    assert isinstance(out["growing"], list)


class _SlowKey:
    """A mapping key whose hash is slow.

    The copier hashes every key it writes into the output dict, so this puts the cost in the
    ORDINARY container walk — the exact frame the wedged gateway was sampled in
    (``_copy_node`` iterating a dict), not in a delegated ``__deepcopy__``.
    """

    def __init__(self, name: str, delay: float):
        self.name = name
        self.delay = delay

    def __hash__(self):
        time.sleep(self.delay)
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, _SlowKey) and other.name == self.name


def test_elapsed_ceiling_fires_during_an_ordinary_walk_of_a_tiny_tree():
    """The wall-clock ceiling must bind on SMALL trees, inside the plain container walk.

    Regression for the gateway wedge: the deadline used to be sampled every 4096th node, so a
    tree with fewer nodes than that never evaluated it even once and ran unbounded. The
    gateway's own config tree is 449 nodes, so its nominal 5s budget was dead code. This is a
    behaviour contract — "a copy that overruns its budget raises" — not a node-count snapshot,
    and the cost sits where the wedge's own stack sat rather than behind ``copy.deepcopy``.
    """
    budget_s = 0.25
    per_node = budget_s / 2
    source = {_SlowKey(f"k{i}", per_node): i for i in range(8)}

    assert count_nodes(source) < 20, "this test is only meaningful on a tiny tree"
    unbounded_cost = per_node * len(source)
    assert unbounded_cost > budget_s * 2, "the copy must be able to overrun its budget"

    started = time.monotonic()
    with pytest.raises(BoundedCopyBreach) as excinfo:
        bounded_deepcopy(source, node_limit=2_000_000, time_budget_s=budget_s)
    elapsed = time.monotonic() - started

    assert excinfo.value.limit == "elapsed"
    # Bounded by the budget plus the one in-flight node it cannot interrupt, and strictly less
    # than letting the walk run to completion: the ceiling actually cut the copy short.
    assert elapsed < unbounded_cost, f"copy ran {elapsed:.2f}s — its ceiling did not bind"
    assert elapsed < budget_s + per_node * 2, f"copy ran {elapsed:.2f}s past its ceiling"


def test_elapsed_ceiling_binds_a_copy_whose_whole_cost_is_one_delegated_node():
    """A value handed to ``copy.deepcopy`` is charged as ONE node; the clock must still bind.

    An arbitrary object's ``__deepcopy__`` can run for any length of time and cannot be
    interrupted from outside, so no per-node check can pre-empt it. The contract is that such
    a copy still RAISES rather than returning a value produced far past its ceiling — which is
    what the caller's fail-closed fallback (``thaw`` of the last good tree) depends on.
    """
    budget_s = 0.25
    slow_per_node = budget_s * 4

    class SlowLeaf:
        """Not a dict/list/tuple/scalar, so the copier delegates to copy.deepcopy."""

        def __deepcopy__(self, memo):
            time.sleep(slow_per_node)
            return SlowLeaf()

    source = {"slow": SlowLeaf()}
    assert count_nodes(source) < 10, "this test is only meaningful on a tiny tree"

    started = time.monotonic()
    with pytest.raises(BoundedCopyBreach) as excinfo:
        bounded_deepcopy(source, node_limit=2_000_000, time_budget_s=budget_s)
    elapsed = time.monotonic() - started

    assert excinfo.value.limit == "elapsed"
    # Bounded by the budget plus the one in-flight node it cannot interrupt, not by a
    # hardcoded duration: the copy must not be allowed to run indefinitely.
    assert elapsed < budget_s + slow_per_node * 2, f"copy ran {elapsed:.2f}s past its ceiling"


def test_node_ceiling_still_reports_the_node_limit_not_the_clock():
    """The added entry/exit clock checks must not relabel a node-limit breach."""
    source = {"small": [1, 2, 3], "huge": [[0] * 50 for _ in range(2000)]}
    with pytest.raises(BoundedCopyBreach) as excinfo:
        bounded_deepcopy(source, node_limit=500, time_budget_s=3600.0)
    assert excinfo.value.limit == "node"
    assert excinfo.value.top_key == "huge"


def test_thaw_has_no_ceilings_and_never_breaches():
    """``thaw`` is the fail-closed fallback for a breached copy; it must never itself breach."""

    class SlowLeaf:
        def __deepcopy__(self, memo):
            time.sleep(0.05)
            return SlowLeaf()

    assert thaw({"slow": SlowLeaf()})["slow"] is not None


def test_breach_names_the_offending_top_level_key():
    """Fail-closed reporting: the breach must name the key an operator has to look at."""
    source = {"small": [1, 2, 3], "huge": [[0] * 50 for _ in range(2000)]}
    with pytest.raises(BoundedCopyBreach) as excinfo:
        bounded_deepcopy(source, node_limit=500)
    assert excinfo.value.top_key == "huge"
    assert "huge" in str(excinfo.value)


def test_bounded_copy_result_is_a_faithful_mutable_copy():
    source = {"a": [1, {"b": (2, 3)}], "c": None, "d": 1.5}
    out = bounded_deepcopy(source)
    assert out == source and out is not source
    assert out["a"] is not source["a"]
    assert type(out["a"]) is list and type(out) is dict
    out["a"].append(4)
    assert source["a"] == [1, {"b": (2, 3)}]


def test_bounded_copy_of_a_view_yields_mutable_containers():
    src = {"a": [1, 2]}
    out = bounded_deepcopy(readonly_view(src), node_limit=copy_budget_for(count_nodes(src)))
    assert type(out) is dict and type(out["a"]) is list
    out["a"].append(3)  # must not raise
    assert src == {"a": [1, 2]}


def test_copies_preserve_sharing_and_survive_cycles():
    """``copy.deepcopy`` memoises, so YAML aliases stay shared and cycles terminate.
    A memo-less replacement turns a valid self-referential document into RecursionError."""
    shared = {"a": 1}
    source = {"first": shared, "second": shared}
    out = bounded_deepcopy(source)
    assert out["first"] == {"a": 1}
    assert out["first"] is out["second"], "aliased nodes must stay shared"
    assert out["first"] is not shared

    cyclic: dict = {}
    cyclic["self"] = cyclic
    for copied in (bounded_deepcopy({"root": cyclic}), thaw({"root": cyclic})):
        assert copied["root"] is copied["root"]["self"]
        assert copied["root"] is not cyclic
    assert count_nodes({"root": cyclic}) >= 2  # must terminate, not recurse
