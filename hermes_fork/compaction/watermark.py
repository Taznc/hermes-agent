"""Per-session compaction watermark (``session-compaction-watermark`` anchor target).

The record lives durably in ``sessions.model_config[KEY]`` and in memory on the session's
``ContextCompressor`` (instance dict, no upstream attribute). Absent record = upstream behaviour, byte for
byte: :func:`effective_threshold_tokens` returns the stored trigger unchanged.

Durable record (``v`` 1)::

    {"v": 1, "tokens": 586800, "context_length": 1000000, "base_threshold_tokens": 484000,
     "ceiling_tokens": 871200, "source": "defer", "updated_at": 1791333286.0}

Validation: anything that is not a dict with ``v == 1``, a positive integer ``tokens`` and a positive
integer ``context_length`` is ignored with one WARNING naming the session and the reason; the session then
compacts at the default threshold. The applied value is always clamped to ``[stored, ceiling]`` where
ceiling = 90% of the usable window (and the auxiliary-summariser ceiling when one is installed), so even a
hand-edited record can neither lower the trigger nor push it past the ceiling.

Resolution (spec §3): ``max(stored, min(record.tokens, ceiling))`` while the record is *active*, i.e. it
belongs to the compressor's bound session id and was computed for the current ``context_length``.
Otherwise it is dormant (kept, not deleted) and the stored trigger is returned.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional, TypeGuard

logger = logging.getLogger(__name__)

KEY = "fork_compaction_watermark"
RECORD_VERSION = 1
# Durable tombstone written by a clear: same behaviour as an absent key, but it stops a rotating-compaction
# child from re-inheriting its parent's record after the user reset the child (JSON ``false``).
CLEARED = False

# Spec Q3: module constants, fractions of the usable input window.
STEP_RATIO = 0.10
CEILING_RATIO = 0.90
HEADROOM_RATIO = 0.05

# Instance-dict slots on the ContextCompressor (fork-owned names; upstream never reads them).
_SLOT = "_fork_wm"
_LOADED_FOR = "_fork_wm_loaded_for"
_WAS_ACTIVE = "_fork_wm_was_active"

# Per-thread reentrancy guard: the active->inactive invalidation re-reads the getter, whose upstream
# recompute calls the getter again from ``_apply_threshold_tokens_cap``. Thread-local so an RPC-thread
# status read can never make the agent thread's concurrent trigger read skip the watermark.
_guard = threading.local()


def _busy() -> set:
    ids = getattr(_guard, "ids", None)
    if ids is None:
        ids = _guard.ids = set()
    return ids


# --------------------------------------------------------------------------- record validation

def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_record(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """``(record, None)`` for a usable record, ``(None, reason)`` otherwise. Pure."""
    if not isinstance(raw, dict):
        return None, f"expected a JSON object, got {type(raw).__name__}"
    if raw.get("v") != RECORD_VERSION:
        return None, f"unsupported version {raw.get('v')!r} (expected {RECORD_VERSION})"
    tokens, window = raw.get("tokens"), raw.get("context_length")
    if not _is_int(tokens) or tokens <= 0:
        return None, f"'tokens' must be a positive integer, got {tokens!r}"
    if not _is_int(window) or window <= 0:
        return None, f"'context_length' must be a positive integer, got {window!r}"
    record: dict[str, Any] = {"v": RECORD_VERSION, "tokens": tokens, "context_length": window}
    for name in ("base_threshold_tokens", "ceiling_tokens"):
        value = raw.get(name)
        record[name] = value if _is_int(value) and value > 0 else None
    record["source"] = raw.get("source") if isinstance(raw.get("source"), str) else "defer"
    updated = raw.get("updated_at")
    record["updated_at"] = float(updated) if isinstance(updated, (int, float)) and not isinstance(updated, bool) else None
    return record, None


def _durable_form(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "session_id"}


# --------------------------------------------------------------------------- window math

def usable_tokens(cc: Any) -> int:
    """Window minus output reservation: the base upstream's own trigger math uses."""
    window = int(cc.context_length or 0)
    effective = getattr(type(cc), "_effective_input_window", None)
    if callable(effective):
        return int(effective(window, getattr(cc, "max_tokens", None)))
    usable = window - int(getattr(cc, "max_tokens", 0) or 0)
    return usable if usable > 0 else window


def ceiling_tokens(cc: Any) -> int:
    """90% of the usable window, further capped by the auxiliary summariser's window when probed."""
    ceiling = int(math.floor(CEILING_RATIO * usable_tokens(cc)))
    aux = getattr(cc, "_aux_context_ceiling", None)
    if _is_int(aux) and 0 < aux < ceiling:
        ceiling = aux
    return ceiling


def _record_applies(cc: Any, record: Optional[dict]) -> bool:
    return (
        record is not None
        and record.get("session_id") == (getattr(cc, "_session_id", "") or "")
        and record.get("context_length") == cc.context_length
    )


# --------------------------------------------------------------------------- anchor target

def _lazy_load(cc: Any, state: dict) -> None:
    """Consult the durable row once per bound session id (spec §3). Never raises."""
    session_id = getattr(cc, "_session_id", "") or ""
    previous_loaded = state.get(_LOADED_FOR)
    if previous_loaded == session_id:
        return
    state[_LOADED_FOR] = session_id  # first, so a reentrant read cannot repeat the load
    if not session_id:
        return
    db = getattr(cc, "_session_db", None)
    # Class-attribute check: a MagicMock session DB (upstream tests) has no such class attribute -> no-op.
    if db is None or not callable(getattr(type(db), "get_session_model_config_value", None)):
        return
    inherited = False
    try:
        raw = db.get_session_model_config_value(session_id, KEY)
        if raw is None:
            parent_id = _continuation_parent_id(db, session_id)
            if parent_id:
                current = state.get(_SLOT)
                if previous_loaded == parent_id:
                    # Same compressor rotated in-process: its in-memory state is authoritative (a defer or a
                    # clear just before rotation may still be queued on the write executor).
                    raw = (_durable_form(current) if current is not None and current.get("session_id") == parent_id
                           else None)
                else:
                    raw = db.get_session_model_config_value(parent_id, KEY)
                inherited = raw is not None
    except Exception:
        logger.debug("compaction watermark load failed for session %s", session_id, exc_info=True)
        return
    if raw is None or raw is CLEARED:
        return
    record, reason = validate_record(raw)
    if record is None:
        logger.warning(
            "Ignoring sessions.model_config[%r] for session %s: %s. This session compacts at the default "
            "threshold; click Reset on the Desktop chip (or skip a compaction again) to rewrite it.",
            KEY, session_id, reason,
        )
        return
    current = state.get(_SLOT)
    if current is None or current.get("session_id") != session_id:
        record["session_id"] = session_id
        state[_SLOT] = record
        if inherited:
            schedule_durable_write(cc, record)  # make the continuation row self-contained


_CONTINUATION_EXCLUDING_MARKERS = ("_branched_from", "_reset_from", "_delegate_from")


def _continuation_parent_id(db: Any, session_id: str) -> Optional[str]:
    """The parent id when *session_id* is a compression continuation of it, else None.

    Legacy rotating compaction (``compression.in_place: false``) publishes the child with the agent's
    session-init ``model_config``, not a copy of the parent's, so the record would otherwise be lost at the
    rotation. Branch / reset / delegate children carry their marker and start at the default.
    """
    getter = getattr(type(db), "get_session", None)
    if not callable(getter):
        return None
    row = db.get_session(session_id) or {}
    parent_id = row.get("parent_session_id")
    if not parent_id:
        return None
    config = row.get("model_config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = None
    if isinstance(config, dict) and any(config.get(m) for m in _CONTINUATION_EXCLUDING_MARKERS):
        return None
    parent = db.get_session(parent_id) or {}
    return parent_id if parent.get("end_reason") == "compression" else None


def effective_threshold_tokens(cc: Any, stored: int) -> int:
    """The ``session-compaction-watermark`` anchor: the trigger every upstream reader sees.

    No record (the default) returns *stored* unchanged. Any failure returns *stored* too.
    """
    try:
        state = vars(cc)
    except TypeError:
        return stored
    busy = _busy()
    if id(cc) in busy:
        return stored
    busy.add(id(cc))
    try:
        _lazy_load(cc, state)
        record = state.get(_SLOT)
        if record is None and not state.get(_WAS_ACTIVE):
            return stored
        if not _record_applies(cc, record):
            if state.get(_WAS_ACTIVE):
                # Active -> inactive (session rebound / window change): while active, the upstream cap may
                # have written a configured cap above the base into the stored value. Re-derive cleanly
                # through the upstream lazy path (same invalidation as the TUI hot-reload).
                state[_WAS_ACTIVE] = False
                cc._threshold_tokens = None
                return cc.threshold_tokens
            return stored
        state[_WAS_ACTIVE] = True
        return max(stored, min(int(record["tokens"]), ceiling_tokens(cc)))
    except Exception:
        logger.debug("compaction watermark resolution failed; using the stored threshold", exc_info=True)
        return stored
    finally:
        busy.discard(id(cc))


def active_record(cc: Any) -> Optional[dict]:
    """The bound session's active record (after the lazy load), else None."""
    _ = cc.threshold_tokens  # runs the anchor: lazy load + transition bookkeeping
    record = vars(cc).get(_SLOT)
    return record if _record_applies(cc, record) else None


def _base_threshold_tokens(cc: Any) -> int:
    """The trigger the config would install for this session right now, without the watermark."""
    busy = _busy()
    busy.add(id(cc))
    try:
        state = vars(cc)
        if state.get(_SLOT) is not None or state.get(_WAS_ACTIVE):
            # The stored value may carry a cap written while the record was active: derive fresh.
            saved = cc._threshold_tokens
            cc._threshold_tokens = None
            try:
                return int(cc.threshold_tokens)
            finally:
                cc._threshold_tokens = saved
        return int(cc.threshold_tokens)
    finally:
        busy.discard(id(cc))


# --------------------------------------------------------------------------- raise planning

@dataclass(frozen=True)
class RaisePlan:
    record: Optional[dict]  # None = refused (at ceiling)
    previous_threshold_tokens: int
    ceiling_tokens: int
    usable_tokens: int
    context_length: int


def plan_raise(cc: Any) -> RaisePlan:
    """Pure planning of one +10% step (spec §1.2); ``record is None`` when refused for headroom."""
    window = int(cc.context_length)
    usable = usable_tokens(cc)
    ceiling = ceiling_tokens(cc)
    step = int(math.ceil(STEP_RATIO * usable))
    headroom = int(math.ceil(HEADROOM_RATIO * usable))
    current = int(cc.threshold_tokens)
    last = getattr(cc, "last_prompt_tokens", 0)
    basis = max(current, last if _is_int(last) else 0)
    new = min(ceiling, basis + step)
    if new - basis < headroom:
        return RaisePlan(None, current, ceiling, usable, window)
    previous = active_record(cc)
    base = previous.get("base_threshold_tokens") if previous else None
    record = {
        "v": RECORD_VERSION, "tokens": int(new), "context_length": window,
        "base_threshold_tokens": base if _is_int(base) else _base_threshold_tokens(cc),
        "ceiling_tokens": ceiling, "source": "defer", "updated_at": time.time(),
        "session_id": getattr(cc, "_session_id", "") or "",
    }
    return RaisePlan(record, current, ceiling, usable, window)


# --------------------------------------------------------------------------- in-memory install / clear

def install_watermark(cc: Any, record: Optional[dict]) -> Optional[dict]:
    """Replace the in-memory record; return the previous one (for rollback). Never touches the DB."""
    state = vars(cc)
    previous = state.get(_SLOT)
    state[_SLOT] = dict(record) if record is not None else None
    if previous is not None:
        # The stored value may hold a cap written while *previous* was active; re-derive it.
        cc._threshold_tokens = None
    return previous


def clear_watermark(cc: Any) -> Optional[dict]:
    """Drop the session's watermark in memory and durably; return the previous record."""
    previous = install_watermark(cc, None)
    schedule_durable_write(cc, None)
    return previous


# --------------------------------------------------------------------------- durable writes

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fork-compaction-wm")
        return _executor


def schedule_durable_write(cc: Any, record: Optional[dict]) -> Optional[Future]:
    """Persist (or tombstone with :data:`CLEARED`, when *record* is None) on the fork's single-worker executor.

    Returns the Future, or None when the compressor has no durable session row to write.
    """
    session_id = getattr(cc, "_session_id", "") or ""
    db = getattr(cc, "_session_db", None)
    if not session_id or db is None or not callable(getattr(type(db), "patch_session_model_config", None)):
        return None
    value = _durable_form(record) if record is not None else CLEARED

    def _write() -> None:
        try:
            db.patch_session_model_config(session_id, {KEY: value})
        except Exception:
            logger.warning("Could not persist the compaction watermark for session %s", session_id, exc_info=True)

    return _get_executor().submit(_write)


# --------------------------------------------------------------------------- status

def watermark_status(cc: Any) -> dict:
    """Chip data: ``{active, watermark_tokens, threshold_tokens, base_threshold_tokens, ceiling_tokens,
    context_length}``."""
    record = active_record(cc)
    threshold = int(cc.threshold_tokens)
    base = record.get("base_threshold_tokens") if record else None
    return {
        "active": record is not None,
        "watermark_tokens": threshold if record is not None else None,
        "threshold_tokens": threshold,
        "base_threshold_tokens": base if _is_int(base) else (None if record else threshold),
        "ceiling_tokens": ceiling_tokens(cc),
        "context_length": int(cc.context_length),
    }
