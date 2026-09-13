"""Fork rate-limit-reset resolution + fallback-chain availability tests
(``hermes_fork.rate_limit_recovery``; anchors in ``agent/conversation_loop.py``,
Phase 2.12).

Exercises the extracted bodies directly through their fork-owned public
names, plus the delegating wrappers in ``agent.conversation_loop`` to prove
the anchor seam still forwards correctly.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from hermes_fork.rate_limit_recovery import (
    fallback_availability,
    resolve_rate_limit_reset_at,
)


def _bucket(limit, remaining_seconds_now):
    return SimpleNamespace(limit=limit, remaining_seconds_now=remaining_seconds_now)


def _rate_limit_state(
    *, has_data=True, provider="anthropic", age_seconds=1.0,
    requests_min=None, requests_hour=None, tokens_min=None, tokens_hour=None,
):
    return SimpleNamespace(
        has_data=has_data,
        provider=provider,
        age_seconds=age_seconds,
        requests_min=requests_min or _bucket(0, 0),
        requests_hour=requests_hour or _bucket(0, 0),
        tokens_min=tokens_min or _bucket(0, 0),
        tokens_hour=tokens_hour or _bucket(0, 0),
    )


class TestResolveRateLimitResetAt:
    """``resolve_rate_limit_reset_at`` epoch resolution."""

    def test_prefers_classifier_error_context_reset_at(self):
        agent = SimpleNamespace(_rate_limit_state=None)
        classified = SimpleNamespace(error_context={"reset_at": 1_700_000_000.0}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) == 1_700_000_000.0

    def test_epoch_vs_delta_disambiguation_treats_context_value_as_absolute_epoch(self):
        """error_context["reset_at"] is always an already-resolved epoch (the classifier does
        the delta-vs-epoch disambiguation before stamping it); this helper must not re-interpret
        a small numeric value there as a relative delta."""
        agent = SimpleNamespace(_rate_limit_state=None)
        # A small-looking value in error_context is still returned verbatim as an epoch,
        # never added to time.time() -- disambiguation is the classifier's job, not this one's.
        classified = SimpleNamespace(error_context={"reset_at": 42.0}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) == 42.0

    def test_millisecond_epoch_from_context_is_returned_as_is(self):
        """This helper performs no unit coercion -- a millisecond epoch is passed through exactly
        as the classifier stamped it (float pass-through contract)."""
        agent = SimpleNamespace(_rate_limit_state=None)
        ms_epoch = 1_700_000_000_000.0
        classified = SimpleNamespace(error_context={"reset_at": ms_epoch}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) == ms_epoch

    def test_bool_reset_at_is_rejected_not_coerced(self):
        """``isinstance(True, int)`` is True in Python -- the bool exclusion guards against a
        stray ``True``/``False`` in error_context being coerced into 1.0/0.0."""
        agent = SimpleNamespace(_rate_limit_state=_rate_limit_state())
        classified = SimpleNamespace(error_context={"reset_at": True}, provider="anthropic")
        # Falls through to the agent state path since the context value is rejected.
        result = resolve_rate_limit_reset_at(agent, classified)
        assert result is None or isinstance(result, float)
        assert result != 1.0

    def test_falls_back_to_fresh_rate_limit_state(self):
        agent = SimpleNamespace(
            _rate_limit_state=_rate_limit_state(
                requests_min=_bucket(limit=60, remaining_seconds_now=30.0),
            )
        )
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        before = time.time()
        result = resolve_rate_limit_reset_at(agent, classified)
        assert result is not None
        assert before + 30.0 <= result <= before + 30.0 + 5.0

    def test_stale_state_older_than_five_minutes_is_rejected(self):
        """The freshness guard (<5 min) prevents surfacing a reset time derived from headers on
        a call that is no longer representative of current rate-limit state."""
        agent = SimpleNamespace(
            _rate_limit_state=_rate_limit_state(
                age_seconds=301.0,
                requests_min=_bucket(limit=60, remaining_seconds_now=30.0),
            )
        )
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None

    def test_state_exactly_at_five_minute_boundary_is_rejected(self):
        agent = SimpleNamespace(
            _rate_limit_state=_rate_limit_state(
                age_seconds=300.0,
                requests_min=_bucket(limit=60, remaining_seconds_now=30.0),
            )
        )
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None

    def test_provider_mismatch_skips_state_fallback(self):
        agent = SimpleNamespace(
            _rate_limit_state=_rate_limit_state(
                provider="openai",
                requests_min=_bucket(limit=60, remaining_seconds_now=30.0),
            )
        )
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None

    def test_no_positive_limit_buckets_returns_none(self):
        agent = SimpleNamespace(_rate_limit_state=_rate_limit_state())
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None

    def test_no_state_and_no_context_returns_none(self):
        agent = SimpleNamespace(_rate_limit_state=None)
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None

    def test_missing_error_context_attribute_falls_back_safely(self):
        agent = SimpleNamespace(_rate_limit_state=None)
        classified = SimpleNamespace(provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None

    def test_exception_in_state_lookup_is_swallowed(self):
        class ExplodingState:
            has_data = True

            def __getattr__(self, name):
                raise RuntimeError("boom")

        agent = SimpleNamespace(_rate_limit_state=ExplodingState())
        classified = SimpleNamespace(error_context={}, provider="anthropic")
        assert resolve_rate_limit_reset_at(agent, classified) is None


class TestFallbackAvailability:
    """``fallback_availability`` tri-state contract: None/True/False."""

    def test_no_chain_configured_returns_none(self):
        agent = SimpleNamespace(_fallback_chain=None)
        assert fallback_availability(agent) is None

    def test_empty_chain_returns_none(self):
        agent = SimpleNamespace(_fallback_chain=[])
        assert fallback_availability(agent) is None

    def test_chain_with_untried_entry_returns_true(self):
        agent = SimpleNamespace(_fallback_chain=["a", "b"], _fallback_index=0)
        assert fallback_availability(agent) is True

    def test_chain_fully_exhausted_returns_false(self):
        agent = SimpleNamespace(_fallback_chain=["a", "b"], _fallback_index=2)
        assert fallback_availability(agent) is False

    def test_missing_fallback_index_defaults_to_zero(self):
        agent = SimpleNamespace(_fallback_chain=["a"])
        assert fallback_availability(agent) is True

    def test_missing_fallback_chain_attribute_returns_none(self):
        agent = SimpleNamespace()
        assert fallback_availability(agent) is None

    def test_exception_is_swallowed_and_returns_none(self):
        class ExplodingAgent:
            @property
            def _fallback_chain(self):
                raise RuntimeError("boom")

        assert fallback_availability(ExplodingAgent()) is None


class TestConversationLoopAnchorDelegates:
    """The anchored wrappers in ``agent.conversation_loop`` forward to the fork module
    unchanged -- proves the relocation kept behavior identical at the call site."""

    def test_resolve_rate_limit_reset_at_wrapper_delegates(self):
        from agent.conversation_loop import _resolve_rate_limit_reset_at

        agent = SimpleNamespace(_rate_limit_state=None)
        classified = SimpleNamespace(error_context={"reset_at": 123.0}, provider="anthropic")
        assert _resolve_rate_limit_reset_at(agent, classified) == 123.0

    def test_fallback_availability_wrapper_delegates(self):
        from agent.conversation_loop import _fallback_availability

        agent = SimpleNamespace(_fallback_chain=["a"], _fallback_index=1)
        assert _fallback_availability(agent) is False
