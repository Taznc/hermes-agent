"""Provider capacity adapters (``agent.kanban_throttle_capacity``).

The contract under test is a trust boundary, so the assertions are mostly about
what must NOT happen: an unsupported provider must never produce a number, a
reading with no denominator must never be turned into a percentage, and nothing
identifying may reach the capability record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import kanban_throttle_capacity as cap
from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
)


# --- Capability contract ------------------------------------------------


def test_codex_is_supported_with_evidence_naming_its_endpoint():
    """AC1: a supported verdict has to say what makes it trustworthy."""
    capability = cap.capability_for("openai-codex")
    assert capability.supported is True
    assert capability.source == "usage_api"
    assert capability.signal == cap.SIGNAL_RATE_LIMIT_WINDOWS
    assert "used_percent" in capability.evidence


def test_nous_is_supported_as_a_credit_balance_not_a_rate_limit():
    """The two signal kinds mean different things; conflating them would let a
    diagnostic claim a rate-limit window where there is only a credit pool."""
    capability = cap.capability_for("nous")
    assert capability.supported is True
    assert capability.signal == cap.SIGNAL_CREDIT_BALANCE
    assert capability.signal != cap.SIGNAL_RATE_LIMIT_WINDOWS


@pytest.mark.parametrize("spelling", ["xai", "xai-oauth", "grok", "x.ai", "X-AI"])
def test_grok_is_unsupported_under_every_spelling(spelling):
    """AC1: Grok must report unsupported with evidence and no values.

    Parametrized over aliases because the danger is a spelling that misses the
    unsupported table and falls through to some generic "supported" answer.
    """
    capability = cap.capability_for(spelling)
    assert capability.supported is False
    assert capability.source is None
    assert capability.signal is None
    assert "404" in capability.evidence


def test_unsupported_provider_yields_no_snapshot_and_never_reaches_the_network():
    """The whole point of the unsupported verdict: no fabricated reading, and
    no fetch attempt either — the capability table settles it outright."""
    assert cap.capability_for("xai").supported is False
    assert cap.fetch_capacity_snapshot("xai") is None


def test_unknown_provider_is_unsupported_rather_than_assumed_readable():
    """Fail closed: a provider nobody has taught this module about cannot be
    assumed to have a signal."""
    capability = cap.capability_for("some-provider-that-does-not-exist")
    assert capability.supported is False
    assert cap.fetch_capacity_snapshot("some-provider-that-does-not-exist") is None


def test_capability_derives_from_the_live_fetcher_registry(monkeypatch):
    """A provider gaining an upstream fetcher must not still read as
    unsupported: the generic path is derived from the registry, not a second
    hardcoded list that can drift from it."""
    from agent import account_usage

    assert cap.capability_for("brand-new-provider").supported is False
    monkeypatch.setitem(
        account_usage._USAGE_FETCHERS, "brand-new-provider", lambda b, k: None
    )
    capability = cap.capability_for("brand-new-provider")
    assert capability.supported is True
    assert capability.source == "account_usage_fetcher"


def test_capability_record_carries_no_credential_shaped_material():
    """AC5: the capability table is an operator-facing surface.

    The patterns require credential LENGTH, not just a prefix: ``xai-oauth`` is
    a provider identifier that legitimately appears here, while a real xAI key
    is ``xai-`` followed by a long opaque string.
    """
    import re

    blob = repr([c.public_state() for c in cap.capability_table()])
    # Bearer tokens, sk-/xai- API keys, JWTs, long opaque blobs.
    assert not re.search(r"(?i)bearer\s+\S", blob)
    assert not re.search(r"\b(?:sk|xai)-[A-Za-z0-9_-]{16,}", blob)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", blob)
    assert not re.search(r"[A-Za-z0-9_-]{40,}", blob)


def test_capability_table_reports_both_verdict_kinds():
    """A table that only listed what works would hide the honest negative,
    which is precisely the thing this card asks to be reported."""
    table = cap.capability_table()
    assert any(c.supported for c in table)
    assert any(not c.supported for c in table)
    assert all(c.evidence.strip() for c in table)


# --- Nous adapter -------------------------------------------------------


class _Sub:
    def __init__(self, **kw):
        self.plan = kw.get("plan", "Plus")
        self.monthly_credits = kw.get("monthly_credits")
        self.credits_remaining = kw.get("credits_remaining")
        self.current_period_end = kw.get("current_period_end")


class _Info:
    def __init__(self, **kw):
        self.logged_in = kw.get("logged_in", True)
        self.source = kw.get("source", "account_api")
        self.fresh = kw.get("fresh", True)
        self.error = kw.get("error")
        self.paid_service_access = kw.get("paid_service_access", True)
        self.subscription = kw.get("subscription")


@pytest.fixture
def portal(monkeypatch):
    """Patch the portal read at the seam the adapter imports it from."""

    def _install(info):
        import hermes_cli.nous_account as na

        monkeypatch.setattr(
            na, "get_nous_portal_account_info", lambda **kw: info, raising=True
        )

    return _install


def test_nous_live_balance_becomes_a_used_percentage(portal):
    """Behaviour contract: used% is the consumed fraction of the real cap."""
    portal(_Info(subscription=_Sub(monthly_credits=20.0, credits_remaining=5.0)))
    snap = cap.nous_capacity_snapshot()
    assert snap is not None and snap.unavailable_reason is None
    (window,) = snap.windows
    assert window.used_percent == pytest.approx(75.0)
    assert window.is_active is True


def test_nous_rollover_above_the_cap_produces_no_number(portal):
    """remaining > cap makes the cap a meaningless denominator.

    Emitting a (negative, or clamped-to-zero) percentage here would report
    spare capacity that was never measured, so the adapter must decline.
    """
    portal(_Info(subscription=_Sub(monthly_credits=10.0, credits_remaining=25.0)))
    snap = cap.nous_capacity_snapshot()
    assert snap is not None
    assert snap.windows == ()
    assert snap.unavailable_reason is not None


def test_nous_cached_jwt_reading_is_refused_as_stale(portal):
    """Entitlement claims are not a balance; acting on them would throttle (or
    fail to throttle) from data that never described current consumption."""
    portal(
        _Info(source="jwt", fresh=False,
              subscription=_Sub(monthly_credits=20.0, credits_remaining=5.0))
    )
    snap = cap.nous_capacity_snapshot()
    assert snap is not None
    assert snap.unavailable_reason == cap.STALE_PORTAL_READING
    assert snap.windows == ()


def test_only_codes_this_module_mints_are_carried_through_by_name():
    """The classifier is the trust boundary for a durable audit row.

    ``unavailable_reason`` carries two different things: an enumerated code
    minted here, and free-form provider prose that ``/usage`` prints. Only the
    former may survive verbatim, or untrusted text ends up in a persisted
    record; anything else must degrade to the caller's generic verdict.
    """
    assert cap.classify_unavailable_reason(cap.STALE_PORTAL_READING) == (
        cap.STALE_PORTAL_READING
    )
    for foreign in (
        None, "", "Account 12345 suspended", "Nous Portal account read failed.",
        f"prefixed {cap.STALE_PORTAL_READING}",
    ):
        assert cap.classify_unavailable_reason(foreign) is None


def test_nous_depleted_access_reads_as_fully_consumed(portal):
    """The Portal stating access is gone is authoritative, and must win even
    when no usable denominator exists to compute a percentage from."""
    portal(_Info(paid_service_access=False, subscription=_Sub()))
    snap = cap.nous_capacity_snapshot()
    assert snap is not None
    (window,) = snap.windows
    assert window.used_percent == 100.0
    assert window.limit_reached is True
    assert snap.limit_reached is True


def test_nous_depleted_access_overrides_a_healthy_looking_balance(portal):
    """A stale-looking positive balance beside an explicit depletion flag must
    not read as headroom."""
    portal(
        _Info(paid_service_access=False,
              subscription=_Sub(monthly_credits=20.0, credits_remaining=18.0))
    )
    snap = cap.nous_capacity_snapshot()
    (window,) = snap.windows
    assert window.limit_reached is True
    assert snap.allowed is not True


def test_nous_logged_out_yields_nothing_rather_than_zero_pressure(portal):
    portal(_Info(logged_in=False))
    assert cap.nous_capacity_snapshot() is None


def test_nous_read_error_is_named_rather_than_silently_absent(portal):
    """"Configured but unreadable" and "not configured" call for different
    operator action, so they must not collapse into the same answer."""
    portal(_Info(error="boom"))
    snap = cap.nous_capacity_snapshot()
    assert snap is not None
    assert snap.unavailable_reason is not None
    assert snap.windows == ()


def test_nous_adapter_never_raises_when_the_portal_explodes(monkeypatch):
    """A capacity read is consulted on the dispatch path; it must not be able
    to wedge dispatch."""
    import hermes_cli.nous_account as na

    def _boom(**kw):
        raise RuntimeError("portal down")

    monkeypatch.setattr(na, "get_nous_portal_account_info", _boom, raising=True)
    assert cap.nous_capacity_snapshot() is None


def test_nous_snapshot_carries_no_account_identifier(portal):
    """AC5, against the adapter that touches the most account metadata."""
    portal(_Info(subscription=_Sub(monthly_credits=20.0, credits_remaining=5.0)))
    snap = cap.nous_capacity_snapshot()
    blob = repr(snap)
    for forbidden in ("user_id", "org_id", "privy", "email", "access_token", "raw_"):
        assert forbidden not in blob


# --- Dispatch of supported providers ------------------------------------


def test_supported_provider_routes_through_the_shared_usage_fetcher(monkeypatch):
    """Codex/Anthropic must keep using the existing authenticated path rather
    than a parallel fetch that could drift from it."""
    from agent import account_usage

    called: list[str] = []
    fetched = AccountUsageSnapshot(
        provider="openai-codex", source="usage_api",
        fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=12.0),),
    )

    def _fetch(provider, **kw):
        called.append(provider)
        return fetched

    monkeypatch.setattr(account_usage, "fetch_account_usage", _fetch, raising=True)
    assert cap.fetch_capacity_snapshot("codex") is fetched
    # The alias resolved to the canonical key before the fetch, so one account's
    # quota can never be read under another account's name.
    assert called == ["openai-codex"]


def test_supported_provider_that_fails_never_raises_onto_the_dispatch_path(monkeypatch):
    """A failing fetch yields None rather than propagating.

    The supported-vs-unsupported distinction is carried by the CAPABILITY, not
    by this return value — which is exactly why both must be consulted: a
    caller that read only the None would conflate "network blipped, retry
    helps" with "this provider will never answer".
    """
    from agent import account_usage

    def _boom(provider, **kw):
        raise RuntimeError("network")

    monkeypatch.setattr(account_usage, "fetch_account_usage", _boom, raising=True)
    assert cap.fetch_capacity_snapshot("openai-codex") is None
    assert cap.capability_for("openai-codex").supported is True
    assert cap.capability_for("xai").supported is False


def test_nous_is_dispatched_to_the_fork_adapter_not_the_usage_fetcher(monkeypatch):
    """`nous` has no entry in ``_USAGE_FETCHERS``; routing it there would
    silently return None forever."""
    from agent import account_usage

    monkeypatch.setattr(
        account_usage, "fetch_account_usage",
        lambda *a, **k: pytest.fail("nous must not use the generic fetcher"),
        raising=True,
    )
    monkeypatch.setattr(
        cap, "nous_capacity_snapshot", lambda: "sentinel", raising=True
    )
    monkeypatch.setitem(cap._ADAPTERS, "nous", cap.nous_capacity_snapshot)
    assert cap.fetch_capacity_snapshot("nous") == "sentinel"


def test_reset_at_is_carried_through_for_a_credit_period(portal):
    """A reset time is what lets an operator tell "wait" from "act"."""
    end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    portal(
        _Info(subscription=_Sub(monthly_credits=20.0, credits_remaining=5.0,
                                current_period_end=end))
    )
    snap = cap.nous_capacity_snapshot()
    (window,) = snap.windows
    assert window.reset_at is not None
