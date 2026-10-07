"""Compaction-only cancel: "Keep full context" (spec t_07c75c42 §1.1, §4).

Sits logically beside ``agent.interrupt_control._fence_cancel_before_commit`` but reaches the same fence slot
from fork-owned code. Fence-only: none of ``hard_interrupt()``'s fan-out (no ``_interrupt_requested``, no
``_hard_interrupt_requested`` event, no active-request abort, no tool-worker signal, no child/Codex
interrupt), so the turn keeps running with the uncompressed transcript. Never blocks on an in-flight commit.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Optional

from hermes_fork.compaction.watermark import install_watermark, plan_raise, schedule_durable_write

logger = logging.getLogger(__name__)

# Bounded wait for a fence lock held by lock setup (not a commit): 5 tries, 50 ms apart (<= 250 ms).
CANCEL_ATTEMPTS = 5
CANCEL_RETRY_SLEEP = 0.05

DEFERRED, TOO_LATE, BUSY, NOT_RUNNING, REFUSED = "deferred", "too_late", "busy", "not_running", "refused"


def _result(status: str, reason: Optional[str] = None, plan: Any = None, *, watermark: Optional[int] = None) -> dict:
    return {
        "status": status,
        "reason": reason,
        "watermark_tokens": watermark,
        "previous_threshold_tokens": getattr(plan, "previous_threshold_tokens", None),
        "ceiling_tokens": getattr(plan, "ceiling_tokens", None),
        "usable_tokens": getattr(plan, "usable_tokens", None),
        "context_length": getattr(plan, "context_length", None),
    }


def _active_fence(agent: Any) -> Any:
    state = vars(agent)
    lock = state.get("_compression_commit_fence_lock") or contextlib.nullcontext()
    with lock:
        return state.get("_active_compression_commit_fence")


def _try_cancel(fence: Any) -> Optional[bool]:
    try_cancel = getattr(type(fence), "try_cancel_before_commit", None)
    if not callable(try_cancel):
        return None
    for attempt in range(CANCEL_ATTEMPTS):
        won: Any = try_cancel(fence)
        if won is not None or bool(getattr(fence, "commit_in_flight", False)):
            return won
        if attempt + 1 < CANCEL_ATTEMPTS:
            time.sleep(CANCEL_RETRY_SLEEP)
    return None


def defer_active_compaction(agent: Any) -> dict:
    """Cancel the active AUTOMATIC compaction before it commits and raise this session's watermark.

    Result ``status``: ``deferred`` | ``too_late`` | ``busy`` | ``not_running`` | ``refused`` (``reason``:
    ``manual`` | ``overflow`` | ``at_ceiling`` | ``unsupported``). Serialized per agent: a double click
    raises once (the second call sees the cancelled fence and answers ``not_running``).
    """
    with _defer_lock(agent):
        return _defer_locked(agent)


_LOCKS_GUARD = threading.Lock()


def _defer_lock(agent: Any) -> Any:
    with _LOCKS_GUARD:
        try:
            return vars(agent).setdefault("_fork_compaction_defer_lock", threading.Lock())
        except TypeError:
            return contextlib.nullcontext()


def _defer_locked(agent: Any) -> dict:
    if getattr(agent, "api_mode", None) == "codex_app_server":
        return _result(REFUSED, "unsupported")
    cc = getattr(agent, "context_compressor", None)
    fence = _active_fence(agent)
    if fence is None or cc is None or bool(getattr(fence, "is_cancelled", False)):
        return _result(NOT_RUNNING)
    trigger = (getattr(cc, "_compression_telemetry_seed", None) or {}).get("trigger_source")
    if trigger == "manual":
        return _result(REFUSED, "manual")
    if trigger == "overflow":
        return _result(REFUSED, "overflow")
    if bool(getattr(fence, "commit_in_flight", False)):
        return _result(TOO_LATE)
    plan = plan_raise(cc)
    if plan.record is None:
        return _result(REFUSED, "at_ceiling", plan)
    # Raise BEFORE cancelling: when the worker unwinds and the turn loop re-checks should_compress(), it must
    # already see the higher trigger, or the cancelled attempt re-arms immediately.
    previous = install_watermark(cc, plan.record)
    won = _try_cancel(fence)
    if won is not True:
        install_watermark(cc, previous)  # rollback to the exact prior record
        status = TOO_LATE if (won is False or bool(getattr(fence, "commit_in_flight", False))) else BUSY
        return _result(status, None, plan)
    schedule_durable_write(cc, plan.record)
    logger.info(
        "Compaction skipped by the user (session=%s); watermark %d -> %d tokens",
        getattr(cc, "_session_id", "") or "none", plan.previous_threshold_tokens, plan.record["tokens"],
    )
    return _result(DEFERRED, None, plan, watermark=int(cc.threshold_tokens))
