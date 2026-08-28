"""Config-cache concurrency contract (#hermes-change-watcher spin).

The ``hermes-change-watcher`` thread polls ``_skin_sig``/``_pet_sig`` every
0.5s, and each tick funnels through ``_load_cfg_raw`` — the process's single
shared config cache. When that cache holds ``_cfg_lock`` *across* a
``copy.deepcopy``, the lock stops being a cheap pointer-swap guard and becomes
a ~90us critical section that every RPC-thread config read must queue behind.
Under concurrent readers that convoy starves the watcher thread (and, through
it, the asyncio accept loop), leaving the backend ``active (running)`` with a
backlog of never-accepted connections.

These tests pin the two invariants that keep the convoy impossible:

  * no ``copy.deepcopy`` may run while ``_cfg_lock`` is held, and
  * ``_load_cfg_raw`` must never hand a caller a reference to the cached
    object (callers mutate what they get and feed it back to ``_save_cfg``).
"""

import copy
import threading

import pytest

from tui_gateway import server


class _TrackingLock:
    """A real lock that also reports whether it is currently held."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False

    def acquire(self, *args, **kwargs):
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired:
            self.held = True
        return acquired

    def release(self):
        self.held = False
        return self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "display:\n  skin: default\n  tui_theme: dark\nagent:\n  reasoning_effort: high\n"
    )
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_mtime", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    monkeypatch.setattr(server, "get_hermes_home_override", lambda: None)
    return tmp_path


def test_deepcopy_never_runs_while_cfg_lock_is_held(cfg_home, monkeypatch):
    """``_cfg_lock`` guards pointer swaps only — never a deepcopy.

    Regression: holding the lock across ``copy.deepcopy(_cfg_cache)`` made
    every config read serialize behind a ~90us critical section, convoying the
    change-watcher thread and starving the event loop.
    """
    lock = _TrackingLock()
    violations = []

    class _CopyShim:
        @staticmethod
        def deepcopy(obj, *args, **kwargs):
            if lock.held:
                violations.append(type(obj).__name__)
            return copy.deepcopy(obj, *args, **kwargs)

    monkeypatch.setattr(server, "_cfg_lock", lock)
    monkeypatch.setattr(server, "copy", _CopyShim)

    server._load_cfg_raw()  # cache MISS  -> populates the cache
    server._load_cfg_raw()  # cache HIT   -> copies the cached dict
    server._save_cfg({"display": {"skin": "default"}})  # write path

    assert violations == [], (
        f"copy.deepcopy ran inside the _cfg_lock critical section {len(violations)} "
        f"time(s) (on {violations}); the lock must only cover reference swaps"
    )


def test_load_cfg_raw_never_returns_the_cached_object(cfg_home):
    """Callers mutate the result and pass it to ``_save_cfg`` — so every return
    path must be a private copy, on a cache miss just as much as a cache hit."""
    for label in ("miss", "hit"):
        cfg = server._load_cfg_raw()
        assert cfg is not server._cfg_cache, f"{label}: returned the live cache object"
        shared = [
            key
            for key, value in cfg.items()
            if isinstance(value, (dict, list)) and value is (server._cfg_cache or {}).get(key)
        ]
        assert not shared, f"{label}: shares mutable sub-objects with the cache: {shared}"

    # A caller mutating its copy must not corrupt the cache for everyone else.
    cfg = server._load_cfg_raw()
    cfg["display"]["skin"] = "mutated-by-caller"
    assert server._cfg_cache["display"]["skin"] == "default"
    assert server._load_cfg_raw()["display"]["skin"] == "default"


def test_concurrent_readers_and_writers_never_observe_a_torn_config(cfg_home):
    """Readers deep-copy outside the lock while writers swap the cache; the
    snapshot each reader gets must always be a complete, self-consistent dict."""
    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                cfg = server._load_cfg_raw()
                # Every snapshot must carry the full key set, never a partial map.
                if set(cfg) != {"display", "agent"}:
                    errors.append(f"torn snapshot: {sorted(cfg)}")
                    return
                cfg["display"]["skin"] = "scribble"  # callers mutate their copy
        except Exception as exc:  # noqa: BLE001 - surface it as a failure
            errors.append(f"{type(exc).__name__}: {exc}")

    def writer():
        try:
            for i in range(60):
                if stop.is_set():
                    return
                server._save_cfg(
                    {"display": {"skin": "default", "tui_theme": "dark"},
                     "agent": {"reasoning_effort": "high", "n": i}}
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    threads.append(threading.Thread(target=writer, daemon=True))
    for thread in threads:
        thread.start()
    threads[-1].join(timeout=30)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
