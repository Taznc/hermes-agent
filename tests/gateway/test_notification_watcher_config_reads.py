"""The notification watcher's drain path must not re-read config.yaml every iteration.

Regression for the dispatcher wedge: ``_drain_watch_notifications`` runs on a 2s timer inside
the event loop and called ``_load_background_notifications_mode``, which rebuilt the ENTIRE
gateway config tree — bounded deepcopy + managed overlay + a full ``${VAR}`` expansion — on
every single tick. These are behaviour contracts about work performed, not snapshots of a
duration or a call count for a fixed tree.
"""

from __future__ import annotations

import asyncio
import os
import queue
import time

import pytest

import gateway.run as gateway_run
import gateway.run_config_loaders as run_config_loaders
import hermes_cli.config as hermes_config
from gateway.run import GatewayRunner


@pytest.fixture
def gateway_home(tmp_path, monkeypatch):
    """A temp HERMES_HOME with a real config.yaml the gateway loaders will read."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    run_config_loaders.invalidate_gateway_runtime_config_view()
    hermes_config._RAW_CONFIG_CACHE.clear()
    yield tmp_path
    run_config_loaders.invalidate_gateway_runtime_config_view()
    hermes_config._RAW_CONFIG_CACHE.clear()


def _write_config(path, mode):
    (path / "config.yaml").write_text(
        f"display:\n  background_process_notifications: {mode}\n", encoding="utf-8")
    # Distinct mtime so a coarse-resolution clock cannot alias two consecutive writes.
    time.sleep(0.01)
    os.utime(path / "config.yaml", None)


class _CountingLoader:
    """Counts the expensive rebuild work the drain path triggers."""

    def __init__(self, monkeypatch):
        self.yaml_parses = 0
        self.deep_copies = 0
        real_parse = hermes_config.fast_safe_load
        real_copy = hermes_config.bounded_deepcopy

        def counting_parse(*args, **kwargs):
            self.yaml_parses += 1
            return real_parse(*args, **kwargs)

        def counting_copy(*args, **kwargs):
            self.deep_copies += 1
            return real_copy(*args, **kwargs)

        monkeypatch.setattr(hermes_config, "fast_safe_load", counting_parse)
        monkeypatch.setattr(hermes_config, "bounded_deepcopy", counting_copy)


def test_drain_loop_does_no_config_work_per_iteration(gateway_home, monkeypatch):
    """Iterations 2..N must parse no YAML and deep-copy no config tree.

    The first call may do whatever work it needs; the contract is that a STEADY-STATE tick
    adds none. With the 2s watcher interval, per-tick work is what starves the event loop.
    """
    _write_config(gateway_home, "concise")
    counter = _CountingLoader(monkeypatch)

    runner = GatewayRunner.__new__(GatewayRunner)
    empty_queue: queue.Queue = queue.Queue()

    asyncio.run(runner._drain_watch_notifications(empty_queue))
    after_first = (counter.yaml_parses, counter.deep_copies)

    for _ in range(50):
        asyncio.run(runner._drain_watch_notifications(empty_queue))

    assert (counter.yaml_parses, counter.deep_copies) == after_first, (
        "the drain path re-read or re-copied the config tree on a steady-state iteration "
        f"(first call: {after_first}, after 50 more: "
        f"{(counter.yaml_parses, counter.deep_copies)})")


def test_drain_loop_still_observes_a_config_change(gateway_home):
    """Caching must not pin a stale value: an edit to config.yaml has to take effect.

    Paired with the test above so neither can be satisfied alone — "does no work" is only
    correct if the value is still live.
    """
    runner = GatewayRunner.__new__(GatewayRunner)

    for mode in ("all", "off", "result", "error", "concise"):
        _write_config(gateway_home, mode)
        assert runner._load_background_notifications_mode() == mode, (
            f"cached config view did not pick up background_process_notifications={mode}")


def test_drain_loop_reexpands_an_env_ref_when_the_environment_rotates(gateway_home, monkeypatch):
    """A ``${VAR}`` value must re-expand when the env moves even though the file does not.

    File mtime/size alone cannot see an in-process credential/setting rotation, so the cache
    key has to carry the referenced env values too.
    """
    (gateway_home / "config.yaml").write_text(
        "display:\n  background_process_notifications: ${HERMES_TEST_NOTIFY_MODE}\n",
        encoding="utf-8")
    runner = GatewayRunner.__new__(GatewayRunner)

    monkeypatch.setenv("HERMES_TEST_NOTIFY_MODE", "result")
    assert runner._load_background_notifications_mode() == "result"

    monkeypatch.setenv("HERMES_TEST_NOTIFY_MODE", "off")  # file untouched
    assert runner._load_background_notifications_mode() == "off"


def test_cached_runtime_config_view_is_read_only(gateway_home):
    """The cached tree is shared by every later reader, so it must not be mutable."""
    from hermes_cli.config_snapshot import FrozenConfigError

    _write_config(gateway_home, "concise")
    view = run_config_loaders._gateway_runtime_config_view()

    assert view == gateway_run._load_gateway_runtime_config()
    with pytest.raises(FrozenConfigError):
        view["display"] = {}
