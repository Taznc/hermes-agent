"""Unit tests for hermes_cli/config_snapshot.py — frozen views and bounded copies.

These are the primitives behind the config-cache fix; their integration with the real config
readers is covered by tests/hermes_cli/test_config_cache_aliasing.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from hermes_cli.config_snapshot import (
    BoundedCopyBreach, FrozenConfigError, bounded_deepcopy, copy_budget_for, freeze, thaw)


def test_frozen_views_keep_isinstance_contracts():
    """~200 readonly call sites branch on isinstance(cfg, dict) — that must keep working."""
    frozen, nodes = freeze({"a": [1, {"b": "c"}], "d": "e"})
    assert isinstance(frozen, dict) and isinstance(frozen["a"], list)
    assert isinstance(frozen["a"][1], dict)
    assert frozen == {"a": [1, {"b": "c"}], "d": "e"}
    assert nodes == 6  # root, list, 1, inner dict, "c", "e"
    assert list(frozen.keys()) == ["a", "d"]
    assert frozen.get("missing") is None


def test_every_container_mutator_is_refused():
    frozen, _ = freeze({"a": [1, 2], "b": {"c": 3}})
    for mutate in (lambda: frozen.__setitem__("x", 1),
                   lambda: frozen.pop("a"),
                   lambda: frozen.update({"x": 1}),
                   lambda: frozen.clear(),
                   lambda: frozen["a"].append(3),
                   lambda: frozen["a"].sort(),
                   lambda: frozen["a"].__setitem__(0, 9),
                   lambda: frozen["b"].setdefault("d", 4)):
        with pytest.raises(FrozenConfigError):
            mutate()
    assert frozen == {"a": [1, 2], "b": {"c": 3}}


def test_thaw_returns_plain_mutable_containers():
    frozen, _ = freeze({"a": [1, {"b": []}]})
    plain = thaw(frozen)
    assert type(plain) is dict and type(plain["a"]) is list and type(plain["a"][1]) is dict
    plain["a"].append(2)
    plain["a"][1]["b"].append(3)
    assert frozen == {"a": [1, {"b": []}]}


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


def test_bounded_copy_of_a_frozen_view_yields_mutable_containers():
    frozen, nodes = freeze({"a": [1, 2]})
    out = bounded_deepcopy(frozen, node_limit=copy_budget_for(nodes))
    assert type(out) is dict and type(out["a"]) is list
    out["a"].append(3)  # must not raise
    assert frozen == {"a": [1, 2]}
