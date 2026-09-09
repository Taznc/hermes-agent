"""Tests for read_raw_config_readonly() — the no-deepcopy raw config read.

The readonly variant exists for per-turn policy checks (e.g. the
shared-metrics gate) that were paying a full config deepcopy on every call.
Contract under test:

1. immutability — the result is a frozen view; mutating it raises rather
   than corrupting the in-process cache (this REPLACED the old identity
   invariant "repeat calls return the SAME cache-owned object", which is
   what let a caller retain a nested cached list and mutate it while
   read_raw_config() deepcopied it — a copy that never terminates and
   wedged a gateway for 16h. Deep-copy behaviour is covered by
   test_config_cache_aliasing.py);
2. freshness — an edited config.yaml (mtime/size change) is picked up;
3. parity — content equals read_raw_config()'s result;
4. missing/broken config degrades to {} exactly like read_raw_config().
"""

import os

import pytest
import yaml


@pytest.fixture()
def isolated_hermes_home():
    """Per-test HERMES_HOME dir (already redirected by the autouse conftest
    fixture) as a Path, with the raw-config cache cleared around the test."""
    from pathlib import Path

    import hermes_cli.config as config_mod

    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config_mod._RAW_CONFIG_CACHE.clear()
    yield home
    config_mod._RAW_CONFIG_CACHE.clear()


def _write_config(home, data):
    cfg = home / "config.yaml"
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    return cfg




def test_readonly_result_is_immutable(isolated_hermes_home):
    """The read-only-view contract that replaced the old identity invariant: a readonly
    caller cannot reach into the cache, at any depth — not through a mutator (which raises,
    FrozenConfigError being a TypeError) and not through object identity (nothing published
    is an object the cache holds, so unbound base-class mutators cannot reach it either)."""
    import hermes_cli.config as config_mod
    from hermes_cli.config import read_raw_config_readonly

    _write_config(isolated_hermes_home,
                  {"telemetry": {"shared_metrics": {"enabled": True}}, "list_key": [1, 2]})
    ro = read_raw_config_readonly()
    assert ro["telemetry"]["shared_metrics"]["enabled"] is True

    (entry,) = list(config_mod._RAW_CONFIG_CACHE.values())
    cached = entry[2]
    assert ro is not cached
    assert ro["telemetry"] is not cached["telemetry"]
    assert ro["list_key"] is not cached["list_key"]

    with pytest.raises(TypeError):
        ro["telemetry"]["shared_metrics"]["enabled"] = False
    with pytest.raises(TypeError):
        ro["list_key"].append(3)

    dict.__setitem__(ro["telemetry"]["shared_metrics"], "enabled", False)
    list.append(ro["list_key"], 3)
    assert cached["telemetry"]["shared_metrics"]["enabled"] is True
    assert cached["list_key"] == [1, 2]

    assert read_raw_config_readonly()["telemetry"]["shared_metrics"]["enabled"] is True
    assert read_raw_config_readonly()["list_key"] == [1, 2]


def test_readonly_matches_the_mutable_variant(isolated_hermes_home):
    """Parity: same content, independent objects."""
    from hermes_cli.config import read_raw_config, read_raw_config_readonly

    _write_config(isolated_hermes_home, {"a": {"b": 1}, "c": [1, 2]})
    mutable = read_raw_config()
    assert mutable == read_raw_config_readonly()

    mutable["a"]["b"] = 999
    mutable["c"].append(3)
    assert read_raw_config_readonly()["a"]["b"] == 1
    assert read_raw_config_readonly()["c"] == [1, 2]


def test_freshness_after_config_edit(isolated_hermes_home):
    from hermes_cli.config import read_raw_config_readonly

    cfg = _write_config(isolated_hermes_home, {"display": {"ephemeral_system_ttl": 1}})
    first = read_raw_config_readonly()
    assert first["display"]["ephemeral_system_ttl"] == 1

    _write_config(isolated_hermes_home, {"display": {"ephemeral_system_ttl": 7}})
    # Force a distinct mtime_ns even on coarse-timestamp filesystems.
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    second = read_raw_config_readonly()
    assert second["display"]["ephemeral_system_ttl"] == 7


def test_missing_config_returns_empty(isolated_hermes_home):
    from hermes_cli.config import read_raw_config_readonly

    cfg = isolated_hermes_home / "config.yaml"
    if cfg.exists():
        cfg.unlink()
    assert read_raw_config_readonly() == {}


