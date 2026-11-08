"""Opt-in delayed Ask projection at the fork-owned PluginUI seam.

No default mailbox path, recipient lookup, extra poller or environment switches.
The lifecycle owner must bind a bridge to the actual turn agent explicitly.
"""
from __future__ import annotations

import contextvars
import logging
import secrets
import sys
import threading
import time

log = logging.getLogger(__name__)
_ATTR = "_fork_human_input_owner"
_source_nonce: contextvars.ContextVar[str | None] = contextvars.ContextVar("fork_input_nonce", default=None)


def source_params(params):
    """Correlate the exact source registration without changing the Ask wire payload."""
    nonce = _source_nonce.get()
    return {**params, "fork_input_nonce": nonce} if nonce else params


def bind_ask_bridge(agent, owner):
    """Bind one live turn/profile owner; pass None to remove it on teardown."""
    setattr(agent, _ATTR, owner)


def route_ask(ui, kind, payload, timeout, send):
    agent = ui._bound_agent()
    owner = getattr(agent, _ATTR, None)
    if owner is None or kind != "fork-ask/questions":
        return send(kind, payload, timeout)
    if getattr(owner, "messaging", False):
        return send(kind, payload, timeout)
    gateway, sid = ui._channel()
    if gateway is not None:
        from tui_gateway import server_requests as source
        snapshots = lambda: source.open_requests(sid)
        watch = lambda rid, expiry: owner.watch_tui(sid, rid, kind="ask", payload=payload, expires_at=expiry, delay_seconds=getattr(owner, "delay_seconds", 120))
    else:
        authority_module = sys.modules.get("hermes_fork.ui.authority")
        route = authority_module.route_for(agent) if authority_module is not None else None
        if route is None:
            return send(kind, payload, timeout)
        authority, sid, _generation = route._turn._approval_owner
        live = authority.sessions.get(sid)
        if live is None:
            return send(kind, payload, timeout)
        from gateway import session_server_requests as source
        events = live.event_stream
        snapshots = lambda: source.open_requests(events, sid)
        watch = lambda rid, expiry: owner.watch_authority(events, sid, rid, kind="ask", payload=payload, expires_at=expiry, delay_seconds=getattr(owner, "delay_seconds", 120))
    nonce = secrets.token_urlsafe(24)
    token = _source_nonce.set(nonce)
    try:
        return _observe(owner, kind, payload, timeout, send, snapshots, watch, nonce)
    finally:
        _source_nonce.reset(token)


def _observe(owner, kind, payload, timeout, send, snapshots, watch, nonce):
    baseline = {r["id"] for r in snapshots()}
    expiry = time.time() + timeout if timeout is not None else None
    stop = threading.Event()
    projected = []
    failures = []

    def monitor():
        try:
            while not stop.is_set():
                if not projected:
                    candidates = [r for r in snapshots() if r["id"] not in baseline
                                  and r["method"] == "plugin.request"
                                  and r["params"].get("fork_input_nonce") == nonce
                                  and r["params"].get("kind") == kind
                                  and r["params"].get("payload") == payload]
                    # Concurrent identical source requests are ambiguous: do not pick a FIFO one.
                    if len(candidates) > 1:
                        raise ValueError("ambiguous Ask source correlation")
                    if candidates:
                        projected.append(watch(candidates[0]["id"], expiry))
                if projected and owner.mailbox.intents(owner.owner_id):
                    owner.drain()
                stop.wait(0.02)
        except Exception as exc:
            failures.append(type(exc).__name__)
            log.warning("Ask mailbox observer stopped (%s)", type(exc).__name__)

    observer = threading.Thread(target=monitor, name="fork-ask-owner", daemon=True)
    observer.start()
    result = None
    try:
        result = send(kind, payload, timeout)
        return result
    finally:
        stop.set()
        observer.join()
        if projected:
            handle = projected[0]
            status = "cancelled"
            if isinstance(result, dict) and not result.get("unsupported"):
                outcome = result.get("outcome", "submitted")
                status = outcome if outcome in {"submitted", "cancelled", "timed_out", "undelivered"} else "cancelled"
            if result is None and expiry is not None and time.time() >= expiry:
                status = "timed_out"
            if failures:
                status = "undelivered"
            owner.settle(handle, status, result)
