"""Regression tests for the shared config caches: aliasing, lock scope, copy bounds.

Ported from the three runnable repros on diagnosis card t_4c2f09a3, which root-caused a 16h
gateway wedge: ``read_raw_config_readonly()`` returned the cache-owned dict, the copy boundary
was only at the top level, so a caller retaining a nested list held the *cached* list — and
``copy.deepcopy`` of a list that grows at least as fast as it is copied never terminates
(``copy._deepcopy_list`` re-reads the length every step). That copy ran inside the module-global
``_CONFIG_LOCK``, so the spin also made every config read in the process unavailable
(measured 10-13s stalls; a second mutable reader died with MemoryError).

Every test here drives the REAL public readers against a temp HERMES_HOME (AGENTS.md: E2E with
real imports), and each was proven RED against the pre-fix implementation on ``dev``.
``FrozenConfigError`` subclasses ``TypeError``, and mutation refusal is asserted as ``TypeError``
so this file collects (and fails behaviourally) against an implementation without it.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as config_mod
from hermes_cli.config import (
    read_raw_config, read_raw_config_readonly, load_config, load_config_readonly)


@pytest.fixture()
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LOAD_CONFIG_CACHE.clear()
    yield home
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LOAD_CONFIG_CACHE.clear()


def _write_config(home: Path, data: dict) -> Path:
    cfg = home / "config.yaml"
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    return cfg


def _cached_raw_tree() -> dict:
    """The private tree ``_RAW_CONFIG_CACHE`` holds — nothing published may be part of it."""
    (entry,) = list(config_mod._RAW_CONFIG_CACHE.values())
    return entry[2]


def _cached_merged_tree() -> dict:
    (entry,) = list(config_mod._LOAD_CONFIG_CACHE.values())
    return entry[4]


# --------------------------------------------------------------------------------------
# repro_a, step 1+2 — the aliasing itself
# --------------------------------------------------------------------------------------

def test_readonly_result_is_not_reachable_from_the_cache(config_home):
    """Nothing the readonly reader publishes may be an object the cache holds, at ANY depth.

    Pre-fix the readonly result WAS the cached dict and a retained nested list WAS the cached
    list, so mutating it silently corrupted the cache for every later reader.

    Object non-identity is the load-bearing assertion, not mutator refusal: a ``dict``
    subclass that merely overrides ``__setitem__`` is still writable through the unbound
    base-class method (``dict.__setitem__(view, ...)``), so a cache-owned subclass would
    remain reachable. Both are asserted here.
    """
    _write_config(config_home, {"command_allowlist": ["ls", "cat"],
                                "approvals": {"deny": ["rm -rf /"]}})

    result = read_raw_config_readonly()
    assert result["command_allowlist"] == ["ls", "cat"]

    cached = _cached_raw_tree()
    assert result is not cached
    assert result["approvals"] is not cached["approvals"]
    assert result["command_allowlist"] is not cached["command_allowlist"]
    assert result["approvals"]["deny"] is not cached["approvals"]["deny"]
    # Two readers never share a container either, so one cannot reach the other's.
    assert result["approvals"] is not read_raw_config_readonly()["approvals"]

    with pytest.raises(TypeError):
        result["command_allowlist"].append("rm")
    with pytest.raises(TypeError):
        result["approvals"]["deny"].clear()
    with pytest.raises(TypeError):
        result["new_key"] = 1

    # The bypass that a mutator-overriding subclass cannot stop: unbound base-class calls.
    # They must land in the throwaway view and be invisible to the cache and every reader.
    dict.__setitem__(result["approvals"], "injected", True)
    list.append(result["approvals"]["deny"], "injected")
    dict.__setitem__(result, "injected_top", True)

    assert "injected" not in cached["approvals"]
    assert cached["approvals"]["deny"] == ["rm -rf /"]
    assert "injected_top" not in cached
    assert read_raw_config_readonly()["approvals"] == {"deny": ["rm -rf /"]}
    assert read_raw_config()["approvals"] == {"deny": ["rm -rf /"]}
    assert read_raw_config_readonly()["command_allowlist"] == ["ls", "cat"]


def test_mutable_reader_is_never_aliased_to_the_cache(config_home):
    """``read_raw_config()`` returns plain, mutable containers detached from the cache."""
    _write_config(config_home, {"command_allowlist": ["ls"], "nested": {"items": [1, 2]}})

    first = read_raw_config()
    assert type(first) is dict and type(first["command_allowlist"]) is list

    first["command_allowlist"].append("rm")
    first["nested"]["items"].append(3)
    assert read_raw_config()["command_allowlist"] == ["ls"]
    assert read_raw_config_readonly()["nested"]["items"] == [1, 2]


def test_load_config_readonly_has_the_same_contract(config_home):
    """``_load_config_impl`` had the identical aliasing + in-lock-copy shape."""
    _write_config(config_home, {"model": {"default": "test-model"},
                                "command_allowlist": ["ls"]})

    ro = load_config_readonly()
    cached = _cached_merged_tree()
    assert ro is not cached
    assert ro["model"] is not cached["model"]
    assert ro["command_allowlist"] is not cached["command_allowlist"]

    with pytest.raises(TypeError):
        ro["command_allowlist"].append("rm")
    with pytest.raises(TypeError):
        ro["model"]["default"] = "other"

    # Base-class bypass must not reach the cache on this path either.
    dict.__setitem__(ro["model"], "default", "injected")
    list.append(ro["command_allowlist"], "injected")
    assert cached["model"]["default"] == "test-model"
    assert cached["command_allowlist"] == ["ls"]

    mutable = load_config()
    assert type(mutable) is dict
    mutable["model"]["default"] = "changed"
    mutable["command_allowlist"].append("rm")
    assert load_config_readonly()["model"]["default"] == "test-model"
    assert load_config_readonly()["command_allowlist"] == ["ls"]


def test_deepcopying_a_readonly_result_yields_mutable_containers(config_home):
    """The documented escape hatch: copy it if you need to write to it."""
    _write_config(config_home, {"nested": {"items": [1, 2]}})

    thawed = copy.deepcopy(read_raw_config_readonly())
    assert type(thawed) is dict and type(thawed["nested"]["items"]) is list
    thawed["nested"]["items"].append(3)  # must not raise
    assert read_raw_config_readonly()["nested"]["items"] == [1, 2]


# --------------------------------------------------------------------------------------
# repro_a, step 3 — a concurrent appender must not make the copy non-terminating
# --------------------------------------------------------------------------------------

def test_copy_terminates_while_a_retained_nested_list_grows(config_home):
    """With an appender racing the copy on a RETAINED READONLY list, ``read_raw_config()``
    must still return.

    This is the wedge, exactly: pre-fix the list retained out of
    ``read_raw_config_readonly()`` WAS the cached list, so appending to it grew the very object
    ``read_raw_config()`` was deepcopying — and ``copy._deepcopy_list`` re-reads the length each
    step, so that copy never returned (the repro hit a 2 GiB cap in 6s).

    Post-fix the append is refused (the view is frozen) so the cache can never be grown from
    outside, and the bounded copy snapshots each list before iterating as a second, independent
    bound. Either way the appender must not stop a reader from returning. The read runs in a
    worker thread with a join deadline so a non-terminating copy FAILS instead of hanging.
    """
    _write_config(config_home, {"growing": list(range(2000))})

    retained = read_raw_config_readonly()["growing"]
    stop = threading.Event()
    reads: list = []

    def _appender():
        while not stop.is_set():
            for _ in range(500):
                try:
                    retained.append(0)
                except TypeError:
                    return  # refused: the cache is unreachable, which is the fix

    def _reader():
        while not stop.is_set():
            out = read_raw_config()
            assert isinstance(out["growing"], list)
            reads.append(1)

    appender = threading.Thread(target=_appender, daemon=True)
    reader = threading.Thread(target=_reader, daemon=True)
    appender.start()
    reader.start()
    time.sleep(3.0)  # let the race run
    stop.set()
    appender.join(timeout=20)
    reader.join(timeout=20)

    assert not reader.is_alive(), (
        "read_raw_config() did not return while a retained nested list was being appended to — "
        "the unbounded in-flight deepcopy")
    assert not appender.is_alive()
    assert reads, "no read completed at all"
    # Whichever way the append went, the cache is intact.
    assert read_raw_config()["growing"] == list(range(2000))
    assert len(read_raw_config_readonly()["growing"]) == 2000


def test_breached_read_falls_back_to_the_last_good_snapshot(config_home, monkeypatch, caplog):
    """On a breach the mutable readers still honour their contract: a plain MUTABLE copy.

    Failing closed must not change what ``read_raw_config()``/``load_config()`` return —
    callers mutate and serialize those results, so handing back a read-only view on the
    fail-safe path would trade a hang for a ``TypeError`` (or a YAML ``RepresenterError``)
    in exactly the degraded situation the fallback exists for. The fallback is a ``thaw`` of
    the same cached tree: unbounded but terminating by construction (every list is
    snapshotted before it is walked, every object memoised).
    """
    from hermes_cli.config_snapshot import BoundedCopyBreach

    _write_config(config_home, {"approvals": {"deny": ["rm -rf /"], "mode": "manual"}})
    read_raw_config_readonly()  # populate the cache
    load_config_readonly()

    def _always_breach(*_args, **_kwargs):
        raise BoundedCopyBreach("approvals", 999, 1.5, "node")

    real_copy = config_mod.bounded_deepcopy
    config_mod.bounded_deepcopy = _always_breach
    try:
        for reader in (read_raw_config, load_config):
            caplog.clear()
            with caplog.at_level("ERROR", logger=config_mod.logger.name):
                served = reader()

            assert served["approvals"]["deny"] == ["rm -rf /"]  # last good values, not {}
            assert type(served) is dict and type(served["approvals"]["deny"]) is list
            served["approvals"]["deny"].append("added")  # must not raise
            served["approvals"]["mode"] = "auto"
            yaml.safe_dump(served)  # callers write these results back out
            assert any("approvals" in rec.getMessage() for rec in caplog.records), caplog.text
    finally:
        config_mod.bounded_deepcopy = real_copy

    # Mutating a breach result must not have reached the cache.
    assert read_raw_config_readonly()["approvals"]["deny"] == ["rm -rf /"]
    assert read_raw_config()["approvals"]["mode"] == "manual"


# --------------------------------------------------------------------------------------
# YAML anchors/aliases and self-references must copy exactly as copy.deepcopy did
# --------------------------------------------------------------------------------------

def test_aliased_and_cyclic_yaml_still_reads(config_home):
    """A self-referential document read fine on ``dev`` (``copy.deepcopy`` memoises); the
    replacement copy must too, or every raw config read raises ``RecursionError``.

    ``root: &root\\n  self: *root`` is the minimal case. Aliases that merely SHARE a node
    (no cycle) must keep their sharing, since that is what ``copy.deepcopy`` guarantees and
    what a caller comparing two sections would observe.
    """
    cfg = config_home / "config.yaml"
    cfg.write_text("root: &root\n  self: *root\n", encoding="utf-8")

    out = read_raw_config()
    assert out["root"] is out["root"]["self"], "the cycle must be preserved, not unrolled"
    assert read_raw_config()["root"]["self"]["self"]["self"] is not None
    # The readonly view mints a wrapper per access, so identity is not the observable here
    # (and ``==`` on two self-referential mappings recurses forever). Walking it must work.
    node = read_raw_config_readonly()["root"]
    for _ in range(50):
        assert list(node.keys()) == ["self"]
        node = node["self"]

    cfg.write_text("shared: &s\n  a: 1\nfirst: *s\nsecond: *s\n", encoding="utf-8")
    config_mod._RAW_CONFIG_CACHE.clear()
    shared = read_raw_config()
    assert shared["first"] == {"a": 1}
    assert shared["first"] is shared["second"], "aliased nodes must stay shared after a copy"


# --------------------------------------------------------------------------------------
# repro_b — a slow copy must not make config reads unavailable process-wide
# --------------------------------------------------------------------------------------

def test_a_slow_copy_does_not_block_independent_readers(config_home):
    """While one thread is inside the copy, independent readers must keep returning.

    Pre-fix the copy ran inside ``_CONFIG_LOCK``, so every other config reader in the process
    serialized behind it (the repro measured 10.2s and 12.9s stalls on readers that should cost
    microseconds).

    The measure is the readonly reader's **p99 latency relative to one copy**, not wall-clock
    and not throughput. It is self-calibrating (the copy is timed in the same run, so a loaded
    machine slows both sides together) and it separates cleanly: measured p99 was 0.10ms on the
    fix versus 79ms on base against a ~50-70ms copy — 0.2% vs 117% of a copy. Throughput ratio
    is NOT usable here: base bursts through the lock between copies and scored 17-133x across
    runs, straddling any fixed threshold. Worst-case latency is not usable either: a pure-Python
    recursive copy holds the GIL between bytecode boundaries, so even the fix shows tens of ms
    at the tail.
    """
    # Big enough that one copy is clearly measurable, small enough to parse fast.
    _write_config(config_home, {"bulk": [{"k": i, "v": [i, i + 1, i + 2]} for i in range(20000)],
                                "approvals": {"mode": "manual"}})
    read_raw_config()  # warm the cache; the parse must not be timed

    started = time.monotonic()
    read_raw_config()
    copy_seconds = time.monotonic() - started
    assert copy_seconds > 0.01, (
        f"one mutable read took only {copy_seconds * 1000:.1f}ms — too fast to distinguish "
        "blocked from unblocked; grow the fixture")

    stop = threading.Event()
    counts = {"mutable": 0}
    latencies: list = []

    def _mutable_reader():
        while not stop.is_set():
            read_raw_config()
            counts["mutable"] += 1

    def _readonly_reader():
        while not stop.is_set():
            at = time.monotonic()
            assert read_raw_config_readonly()["approvals"]["mode"] == "manual"
            latencies.append(time.monotonic() - at)

    threads = [threading.Thread(target=_mutable_reader, daemon=True),
               threading.Thread(target=_readonly_reader, daemon=True)]
    for thread in threads:
        thread.start()
    time.sleep(3.0)
    stop.set()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert counts["mutable"] >= 2, f"the copying thread barely ran: {counts}"
    assert len(latencies) >= 100, f"too few readonly samples to take a p99: {len(latencies)}"
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)]
    assert p99 < copy_seconds * 0.25, (
        f"readonly p99 latency {p99 * 1000:.1f}ms is {p99 / copy_seconds:.0%} of one copy "
        f"({copy_seconds * 1000:.1f}ms) — readers are serializing behind the copy, i.e. the "
        f"copy is holding the global config lock ({counts['mutable']} copies, "
        f"{len(latencies)} readonly reads)")


def test_load_config_readonly_is_served_from_outside_the_copy(config_home, monkeypatch):
    """Same lock-scope contract on the merged-config path, asserted deterministically.

    Blocks inside the copy step and proves an independent readonly load still returns. (Unlike
    the test above this patches the fix's own seam, so it pins the contract going forward rather
    than re-proving the base defect.)
    """
    _write_config(config_home, {"model": {"default": "test-model"}})
    load_config_readonly()

    inside_copy = threading.Event()
    release_copy = threading.Event()
    real_copy = config_mod.bounded_deepcopy

    def _slow_copy(source, **kwargs):
        inside_copy.set()
        release_copy.wait(timeout=30)
        return real_copy(source, **kwargs)

    monkeypatch.setattr(config_mod, "bounded_deepcopy", _slow_copy)
    slow = threading.Thread(target=load_config, daemon=True)
    slow.start()
    try:
        assert inside_copy.wait(timeout=10), "slow copy never started"
        started = time.monotonic()
        assert load_config_readonly()["model"]["default"] == "test-model"
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"independent readonly load blocked {elapsed:.1f}s"
    finally:
        release_copy.set()
        slow.join(timeout=30)
    assert not slow.is_alive()


# --------------------------------------------------------------------------------------
# freshness must survive the frozen cache
# --------------------------------------------------------------------------------------

def test_freshness_still_works_through_the_frozen_cache(config_home):
    """An edited config.yaml must still be picked up (signature-keyed cache)."""
    cfg = _write_config(config_home, {"display": {"ephemeral_system_ttl": 1}})
    assert read_raw_config_readonly()["display"]["ephemeral_system_ttl"] == 1

    _write_config(config_home, {"display": {"ephemeral_system_ttl": 7}})
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert read_raw_config_readonly()["display"]["ephemeral_system_ttl"] == 7
    assert read_raw_config()["display"]["ephemeral_system_ttl"] == 7
