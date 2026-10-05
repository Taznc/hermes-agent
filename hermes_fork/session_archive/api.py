"""Observe queued API work through the built-in run-status control plane."""

import threading
import weakref
from functools import wraps

from .activity import home_matches, owner_home

_lock = threading.RLock()
_adapters = weakref.WeakKeyDictionary()
ACTIVE_RUN_STATES = {"queued", "running", "waiting_for_approval", "stopping"}


def protect_run_status(fn):
    @wraps(fn)
    def wrapped(adapter, run_id, status, **fields):
        with _lock:
            owners = _adapters.setdefault(adapter, {})
            owners.setdefault(run_id, owner_home())
            return fn(adapter, run_id, status, **fields)

    return wrapped


def blockers(db, ids):
    # Caller holds _lock until its SessionDB transaction commits/rolls back.
    for adapter, owners in _adapters.items():
        for run_id, record in adapter._run_statuses.items():
            if (
                record.get("status") in ACTIVE_RUN_STATES
                and record.get("session_id") in ids
                and home_matches(owners.get(run_id), db)
            ):
                return ["queued_or_waiting_api_run"]
    return []
