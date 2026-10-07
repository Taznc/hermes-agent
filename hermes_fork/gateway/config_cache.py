"""Detach gateway raw config copies outside its cache-publication lock.

Install via the existing fork gateway registry; behavioral/effective reads
are deliberately untouched. Published snapshots are never mutated by this adapter.
"""
from __future__ import annotations

import copy
from functools import wraps
import threading


def install(server) -> None:
    """Bind the real raw reader and saver once, retaining the server's cache/lock."""
    # The registry also supports generic RPC-only hosts without this raw-cache seam.
    if not all(callable(getattr(server, name, None)) for name in ("_load_cfg_raw", "_save_cfg")):
        return
    if getattr(server._load_cfg_raw, "_fork_config_cache", False):
        return

    # Serialize saver I/O separately: otherwise A can publish its payload with
    # B's post-replace signature. Readers still use only the existing _cfg_lock.
    save_lock = threading.Lock()

    @wraps(server._load_cfg_raw)
    def load_raw() -> dict:
        from hermes_cli.config import read_user_config_raw
        from hermes_cli.config_read_errors import FailedConfigRead

        try:
            path = server._active_config_path()
            sig = server.file_signature(path.stat()) if path.exists() else None
            with server._cfg_lock:
                cached = server._cfg_cache
                snapshot = (cached if cached is not None
                            and server._cfg_sig == sig and server._cfg_path == path else None)
            if snapshot is not None:
                return copy.deepcopy(snapshot)
            data = read_user_config_raw(path) if path.exists() else {}
        except Exception as exc:
            return FailedConfigRead(error=exc)
        snapshot = copy.deepcopy(data)
        with server._cfg_lock:
            # A concurrent miss/profile switch or saver already published newer
            # state. Return our detached read, but don't replace that snapshot.
            if server._cfg_cache is cached:
                server._cfg_cache, server._cfg_sig, server._cfg_path = snapshot, sig, path
        return data

    @wraps(server._save_cfg)
    def save(cfg: dict):
        from hermes_cli.config import atomic_config_replace
        from hermes_cli.config_read_errors import _refuse_failed_read

        path = server._active_config_path()
        _refuse_failed_read(path, cfg)  # refuse before even trying to copy a fallback
        snapshot = copy.deepcopy(cfg)
        with save_lock:
            atomic_config_replace(path, snapshot)
            try:
                sig = server.file_signature(path.stat())
            except Exception:
                sig = None
            with server._cfg_lock:
                server._cfg_cache, server._cfg_sig, server._cfg_path = snapshot, sig, path

    setattr(load_raw, "_fork_config_cache", True)
    server._load_cfg_raw, server._save_cfg = load_raw, save
