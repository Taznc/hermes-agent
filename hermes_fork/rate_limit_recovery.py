"""Fork-owned rate-limit-recovery helpers extracted from
``agent/conversation_loop.py`` behind ``# >>> FORK ANCHOR`` seams (Phase 2.12).

These bodies resolve a rate-limit reset epoch and fallback-chain availability
for the wire billing/rate-limit failure result. They live in their own module
rather than ``hermes_fork/state_limits.py`` because that module owns
session-DB config parsing and SQL projection builders — a different topic
from in-memory turn-loop recovery state (``agent._rate_limit_state`` /
``agent._fallback_chain``). Public names stay importable from
``agent.conversation_loop`` so callers and tests keep the pre-extraction
import paths; only the fork-owned bodies live here.
"""

from __future__ import annotations

import time
from typing import Any, Optional


def resolve_rate_limit_reset_at(agent: Any, classified: Any) -> Optional[float]:
    """Best-effort epoch-seconds reset time for a rate-limited terminal failure (Phase 2.12; see
    agent/error_surface.py's wire ``reset_at``). Source order: the classifier's own parsed reset
    (Retry-After / body fields on the response that failed — agent.error_classifier's
    ``_extract_reset_epoch_seconds``, stamped into ``classified.error_context["reset_at"]``); else
    ``agent._rate_limit_state`` (headers captured from a PRIOR successful call on the same
    provider), only when fresh (<5 min). ``None`` when nothing usable exists so the wire key is
    omitted entirely."""
    try:
        ctx_reset = (getattr(classified, "error_context", None) or {}).get("reset_at")
        if isinstance(ctx_reset, (int, float)) and not isinstance(ctx_reset, bool):
            return float(ctx_reset)
    except Exception:
        pass
    try:
        state = getattr(agent, "_rate_limit_state", None)
        if state is not None and getattr(state, "has_data", False):
            state_provider = (getattr(state, "provider", "") or "").strip().lower()
            classified_provider = (getattr(classified, "provider", "") or "").strip().lower()
            provider_match = (
                not state_provider or not classified_provider or state_provider == classified_provider)
            if provider_match and state.age_seconds < 300:
                buckets = [
                    b for b in (state.requests_min, state.requests_hour, state.tokens_min, state.tokens_hour)
                    if b.limit > 0]
                if buckets:
                    return time.time() + min(b.remaining_seconds_now for b in buckets)
    except Exception:
        pass
    return None


def fallback_availability(agent: Any) -> Optional[bool]:
    """Tri-state fallback-chain visibility for the wire ``fallback_available`` field. True: the
    chain has an untried entry for THIS turn; False: a chain was configured but every entry was
    tried; None (caller omits the key): no chain was ever configured — see agent/error_surface.py
    on why omission carries that distinct meaning."""
    try:
        chain = getattr(agent, "_fallback_chain", None)
        if not chain:
            return None
        return getattr(agent, "_fallback_index", 0) < len(chain)
    except Exception:
        return None
