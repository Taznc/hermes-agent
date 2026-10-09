"""Plugin activation reaches every serve backend on the host, not only the host owner.

Upstream publishes ONE ``serve`` host record per OS user (``gateway/host_rendezvous.py``). The first
backend to take the host lock owns it; a second non-Desktop backend binds anyway, logs
"observe-only" and publishes nothing (deliberate: the attach/refuse ladder must see one owner,
upstream #119824/#120080). ``hermes plugins enable/install/update`` then asks for the in-process
half of activation through that one record (:func:`hermes_cli.plugins_activation.notify_serve_backend`),
so the second backend never loads the plugin and its new chats miss the plugin's tools. On this
fork's host the second backend is the web-Desktop ``hermes serve --port 9220`` beside the
supervised dashboard.

The host record is left alone. This module adds a side channel in the same 0700 rendezvous dir:

* ``serve-peer-record`` FORK ANCHOR (``hermes_cli.web_server._publish_host_rendezvous``, the
  HELD_BY_OTHER branch): :func:`publish_serve_peer` writes ``fork-serve-peer-<pid>.json`` (0600)
  with this backend's port and session token, removed again on exit.
* ``serve-peer-activation`` FORK ANCHOR (``hermes_cli.plugins_activation.activate_plugin_now``):
  :func:`notify_serve_peers` sends the same activate POST to every other live backend — each peer
  record, and the Desktop-child record that upstream skips once a host owner exists.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PEER_PREFIX = "fork-serve-peer-"
ACTIVATE_PATH = "/api/dashboard/agent-plugins/activate"

_published: List[Path] = []
_atexit_armed = False
_prev_handlers: Dict[int, Any] = {}


def _state_dir() -> Path:
    from gateway import host_rendezvous as hr

    return hr.host_state_dir()


def peer_path(pid: int) -> Path:
    return _state_dir() / f"{PEER_PREFIX}{int(pid)}.json"


def publish_serve_peer(host: str, port: Optional[int], token: str) -> Optional[Path]:
    """Record this backend as a serve peer so plugin activation can reach it. Never raises."""
    if not port or not token:
        return None
    try:
        from gateway import host_rendezvous as hr

        hr.ensure_host_state_dir()
        path = peer_path(os.getpid())
        payload = {"pid": os.getpid(), "createTime": hr.process_create_time(),
                   "host": str(host or ""), "port": int(port), "token": token}
        hr._write_private_text(path, json.dumps(payload))
    except Exception:
        logger.warning("serve peer record could not be published; `hermes plugins enable` will not "
                       "reach this backend", exc_info=True)
        return None
    _arm_cleanup(path)
    return path


def live_peers() -> List[Dict[str, Any]]:
    """Every peer record whose owner is alive or unprovable; provably dead ones are removed."""
    from gateway import host_rendezvous as hr

    peers: List[Dict[str, Any]] = []
    for path in sorted(_state_dir().glob(f"{PEER_PREFIX}*.json")):
        if not hr._record_is_own(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        pid, port = data.get("pid"), data.get("port")
        if not isinstance(pid, int) or pid <= 0 or not isinstance(port, int) or not 0 < port <= 65535:
            continue
        create = data.get("createTime")
        if hr._pid_incarnation_matches(pid, float(create) if isinstance(create, (int, float)) else None) is False:
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        peers.append({"pid": pid, "host": str(data.get("host") or ""), "port": port,
                      "token": str(data.get("token") or "")})
    return peers


def _dial(host: str) -> str:
    """Same rule as ``host_rendezvous.dial_host``: a wildcard bind is reached over loopback."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "*", "") else host


def _targets(upstream_dialed: bool) -> List[Tuple[str, int, str]]:
    """(host, port, token) of every live backend other than this process and, when
    ``upstream_dialed``, the one upstream's single notify already reached."""
    from gateway import host_rendezvous as hr
    from hermes_cli.plugins_activation import _serve_backend_record

    seen = {os.getpid()}
    dialed = _serve_backend_record() if upstream_dialed else None
    if dialed is not None:
        seen.add(dialed.pid)
    targets: List[Tuple[str, int, str]] = []
    for role in (hr.ROLE_SERVE, hr.ROLE_DESKTOP_SERVE):
        record = hr.read_record(role)
        if record is None or not record.port or record.pid in seen or not hr.record_token_is_consistent(record):
            continue
        seen.add(record.pid)
        targets.append((hr.dial_host(record), record.port, hr.read_token(role)))
    for peer in live_peers():
        if peer["pid"] in seen:
            continue
        seen.add(peer["pid"])
        targets.append((_dial(peer["host"]), peer["port"], peer["token"]))
    return targets


def _post_activate(host: str, port: int, token: str, name: str, home: Path) -> Optional[Dict[str, Any]]:
    import urllib.request

    request = urllib.request.Request(
        f"http://{host}:{port}{ACTIVATE_PATH}",
        data=json.dumps({"name": name, "home": str(home)}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Hermes-Session-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 — loopback http
            answer = json.loads(response.read().decode("utf-8"))
    except Exception:
        logger.debug("serve peer %s:%s did not activate %r", host, port, name, exc_info=True)
        return None
    return answer if isinstance(answer, dict) else None


def notify_serve_peers(name: str, home: Path, activation: Optional[Dict[str, Any]], *,
                       upstream_dialed: bool) -> Optional[Dict[str, Any]]:
    """Ask every other live serve backend to run ``load_and_go_live(name)`` for ``home``.

    ``upstream_dialed``: the caller is a CLI process whose upstream notify already reached the host
    record's backend (skip it); False for an in-process activation inside one backend (the
    dashboard / Desktop plugin UI), whose siblings upstream never tells.
    ``activation`` is the caller's result; it is kept when set, else the first peer's answer fills
    it (so the CLI still reports what went live when only a peer answered). Never raises.
    """
    try:
        targets = _targets(upstream_dialed)
    except Exception:
        logger.debug("serve peer discovery failed", exc_info=True)
        return activation
    for host, port, token in targets:
        answer = _post_activate(host, port, token, name, home)
        if activation is None and answer is not None:
            activation = answer.get("activation")
    return activation


def remove_own_peer_records() -> None:
    for path in tuple(_published):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    _published.clear()


def _arm_cleanup(path: Path) -> None:
    """Remove the record (it carries a live session token) on exit, SIGTERM included.

    Mirrors ``host_rendezvous.cleanup_on_exit``: systemd stop sends SIGTERM, which ends in the
    default disposition without running ``atexit``, so the cleanup is prepended to the installed
    handler and then hands off to it.
    """
    global _atexit_armed
    if path not in _published:
        _published.append(path)
    if not _atexit_armed:
        import atexit

        atexit.register(remove_own_peer_records)
        _atexit_armed = True
    _prepend_terminate_handler()


def _prepend_terminate_handler() -> None:
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return
    for sig_name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, sig_name, None)
        if signum is None or signum in _prev_handlers:
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            prev = signal.getsignal(signum)
            signal.signal(signum, _on_terminate)
            _prev_handlers[signum] = prev


def _on_terminate(signum, frame) -> None:
    import signal

    remove_own_peer_records()
    prev = _prev_handlers.get(signum)
    if callable(prev):
        prev(signum, frame)
        return
    if prev is signal.SIG_IGN:
        return
    with contextlib.suppress(Exception):
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(128 + int(signum))
