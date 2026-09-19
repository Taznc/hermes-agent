"""Admission control's consumption of provider capacity adapters.

Covers the seam between :mod:`agent.kanban_throttle_capacity` and
:mod:`agent.kanban_throttle`: exhaustion semantics, the unsupported-provider
degraded state, and the guarantee that an unusable reading mutates no route.

The unsupported-provider cases deliberately do NOT fake a verdict — they name
``xai``, which the real capability table reports as unsupported on its own
evidence. Stubbing that answer would let the table drift to "supported" while
these tests stayed green, which is precisely the failure this card exists to
prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import kanban_throttle as kt
from agent import kanban_throttle_capacity as cap
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow


@pytest.fixture(autouse=True)
def isolated_throttle_home(tmp_path, monkeypatch):
    """Pin the throttle's coordination DB inside tmp_path.

    ``throttle_state_db_path`` resolves through ``kanban_home()``, and the
    dispatcher exports ``HERMES_KANBAN_*`` into every worker env, so the env
    must be cleared or a "sandboxed" test writes the LIVE board's throttle
    state (fork-dev-workflow §6e).
    """
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_PIN_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "kanban-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    # The assert is the load-bearing part: env hygiene is easy to get subtly
    # wrong and the failure is silent and destructive.
    resolved = kt.throttle_state_db_path()
    assert str(resolved).startswith(str(tmp_path)), f"NOT ISOLATED: {resolved}"
    kt.reset_signal_cache()
    yield
    kt.reset_signal_cache()


@pytest.fixture
def cfg():
    return kt.load_throttle_config({})


def _snapshot(**kw) -> AccountUsageSnapshot:
    kw.setdefault("provider", "openai-codex")
    kw.setdefault("source", "usage_api")
    kw.setdefault("fetched_at", datetime.now(timezone.utc))
    return AccountUsageSnapshot(**kw)


@pytest.fixture
def capacity(monkeypatch):
    """Install a fake snapshot at the authenticated adapter seam.

    Patched on ``kanban_throttle_capacity`` because the throttle late-imports
    ``fetch_capacity_snapshot`` from there inside the function. Only the
    NETWORK half is faked: whether a provider is supported at all still comes
    from the real capability table.
    """

    def _install(result):
        kt.reset_signal_cache()
        monkeypatch.setattr(
            cap, "fetch_capacity_snapshot",
            lambda provider: result(provider) if callable(result) else result,
            raising=True,
        )

    return _install


# --- Exhaustion semantics -----------------------------------------------


def test_account_level_limit_reached_reads_as_full_pressure(cfg, capacity):
    """A provider stating the account is blocked outranks the percentage it
    reports beside that statement.

    Codex serves exactly this: ``limit_reached`` true with a low
    ``used_percent``. Trusting the number would admit workers onto a closed
    account.
    """
    capacity(_snapshot(
        limit_reached=True,
        windows=(AccountUsageWindow(label="Session", used_percent=3.0),),
    ))
    signal = kt.capacity_signal("openai-codex", cfg=cfg)
    assert signal.fresh is True
    assert signal.used_percent == 100.0
    assert signal.reason == "provider_limit_reached"


def test_allowed_false_reads_as_full_pressure(cfg, capacity):
    """``allowed`` is the other half of the same statement."""
    capacity(_snapshot(
        allowed=False,
        windows=(AccountUsageWindow(label="Session", used_percent=5.0),),
    ))
    assert kt.capacity_signal("openai-codex", cfg=cfg).used_percent == 100.0


def test_exhausted_account_with_no_parseable_window_is_still_full(cfg, capacity):
    """"Provider says it is closed" is a stronger fact than "no window
    parsed" — without this the board would hold its old state while the
    account is provably spent."""
    capacity(_snapshot(allowed=False, windows=()))
    signal = kt.capacity_signal("openai-codex", cfg=cfg)
    assert signal.fresh is True
    assert signal.used_percent == 100.0


def test_per_window_limit_reached_wins_over_its_own_percentage(cfg, capacity):
    capacity(_snapshot(windows=(
        AccountUsageWindow(label="Weekly", used_percent=8.0, limit_reached=True),
    )))
    signal = kt.capacity_signal("openai-codex", cfg=cfg)
    assert signal.used_percent == 100.0
    assert signal.window_label == "Weekly"


def test_an_inactive_exhausted_window_is_still_skipped(cfg, capacity):
    """``is_active is False`` is the provider saying the window is not
    counting; a stale limit flag on it must not manufacture pressure."""
    capacity(_snapshot(windows=(
        AccountUsageWindow(label="Old", used_percent=10.0, limit_reached=True,
                           is_active=False),
        AccountUsageWindow(label="Now", used_percent=20.0, is_active=True),
    )))
    signal = kt.capacity_signal("openai-codex", cfg=cfg)
    assert signal.used_percent == 20.0
    assert signal.window_label == "Now"


def test_healthy_snapshot_is_unaffected_by_the_exhaustion_path(cfg, capacity):
    """Regression guard: the ordinary case must still report the worst active
    window verbatim."""
    capacity(_snapshot(allowed=True, limit_reached=False, windows=(
        AccountUsageWindow(label="Session", used_percent=19.0, is_active=True),
        AccountUsageWindow(label="Weekly", used_percent=64.0, is_active=True),
    )))
    signal = kt.capacity_signal("openai-codex", cfg=cfg)
    assert (signal.used_percent, signal.window_label) == (64.0, "Weekly")
    assert signal.reason is None


# --- Unsupported providers ----------------------------------------------


def test_unsupported_provider_is_reported_by_name_not_as_a_fetch_failure(cfg):
    """AC1/AC4: the operator must be able to tell "cannot ever read" from
    "did not read this time"."""
    signal = kt.capacity_signal("xai", cfg=cfg)
    assert signal.fresh is False
    assert signal.used_percent is None
    assert signal.reason == cap.UNSUPPORTED_PROVIDER


def test_unsupported_provider_is_settled_without_any_fetch(cfg, monkeypatch):
    """The verdict is a property of the provider, so it must cost no network
    call — an unsupported source cannot become a per-tick timeout budget."""
    monkeypatch.setattr(
        cap, "fetch_capacity_snapshot",
        lambda provider: pytest.fail(f"fetched unsupported provider {provider!r}"),
        raising=True,
    )
    assert kt.capacity_signal("xai", cfg=cfg).reason == cap.UNSUPPORTED_PROVIDER


def test_all_sources_unsupported_degrades_with_the_actionable_hint(cfg):
    """Pointing at `hermes /usage` for a provider that serves no quota
    document sends the operator chasing a reading that cannot exist."""
    decision = kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["xai"]}},
        now=1_000,
    )
    assert decision.degraded is True
    assert decision.degraded_reason == kt.DEGRADED_UNSUPPORTED_SOURCE
    assert "source_providers" in (decision.recovery_hint or "")
    assert decision.public_state()["recovery"] == decision.recovery_hint


def test_a_readable_provider_beside_an_unsupported_one_still_drives_state(capacity):
    """One bad entry in ``source_providers`` must not blind the throttle to a
    provider that does report."""
    capacity(lambda provider: _snapshot(provider="anthropic", windows=(
        AccountUsageWindow(label="Weekly", used_percent=75.0, is_active=True),
    )) if provider == "anthropic" else None)
    decision = kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["xai", "anthropic"]}},
        now=2_000,
    )
    assert decision.degraded is False
    assert decision.pressure_percent == 75.0
    assert decision.state == kt.STATE_REDUCE


def test_mixed_unsupported_and_failed_fetch_uses_the_generic_hint(capacity):
    """Only an ALL-unsupported read is a configuration error; a provider that
    could have answered but did not is the ordinary transient case."""
    capacity(lambda provider: None)
    decision = kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["xai", "anthropic"]}},
        now=3_000,
    )
    assert decision.degraded_reason == kt.DEGRADED_NO_SIGNAL
    assert "/usage" in (decision.recovery_hint or "")


# --- No route mutation without a signal ---------------------------------


def test_unsupported_source_mutates_no_route():
    """AC4: a degraded state leaves every spawn's route exactly as filed."""
    decision = kt.evaluate_throttle(
        kanban_cfg={
            "usage_throttle": {
                "source_providers": ["xai"],
                "levers": {"downgrade_model": {
                    "enabled": True, "threshold_pct": 1,
                    "ladder": ["expensive", "cheap"],
                }},
            }
        },
        now=4_000,
    )
    assert decision.degraded is True
    plan = kt.plan_route_change(
        decision, assignee="worker", model="expensive", provider="anthropic",
    )
    assert plan is None


def test_unsupported_failover_destination_declines_the_reroute(capacity):
    """A destination with no capacity contract is unknown, not spare
    capacity."""
    capacity(lambda provider: _snapshot(provider="anthropic", windows=(
        AccountUsageWindow(label="Weekly", used_percent=97.0, is_active=True),
    )) if provider == "anthropic" else None)
    decision = kt.evaluate_throttle(
        kanban_cfg={
            "usage_throttle": {
                "source_providers": ["anthropic"],
                "levers": {"cross_provider_failover": {
                    "enabled": True, "threshold_pct": 95,
                    "eligible_profiles": ["overflow-worker"],
                    "destination": {"provider": "xai", "model": "grok",
                                    "max_pressure_pct": 50},
                }},
            }
        },
        now=5_000,
    )
    assert decision.pressure_percent == 97.0
    assert kt.failover_eligible_profiles(decision, now=5_000) == ()
    assert kt.plan_failover(decision, assignee="overflow-worker", now=5_000) is None


def test_declined_unsupported_destination_is_audited_once_per_episode(capacity):
    """The refusal has to be visible, and visible again after recovery — but
    not once per spawn in between."""
    capacity(lambda provider: _snapshot(provider="anthropic", windows=(
        AccountUsageWindow(label="Weekly", used_percent=97.0, is_active=True),
    )) if provider == "anthropic" else None)
    kanban_cfg = {
        "usage_throttle": {
            "source_providers": ["anthropic"],
            "levers": {"cross_provider_failover": {
                "enabled": True, "threshold_pct": 95,
                "eligible_profiles": ["overflow-worker"],
                "destination": {"provider": "xai", "max_pressure_pct": 50},
            }},
        }
    }
    decision = kt.evaluate_throttle(kanban_cfg=kanban_cfg, now=6_000)
    for _ in range(5):
        kt.failover_eligible_profiles(decision, now=6_000)
    declined = [
        e for e in kt.recent_throttle_events(50)
        if e["payload"].get("reason") == kt.DEGRADED_FAILOVER_DESTINATION
    ]
    assert len(declined) == 1
    assert declined[0]["payload"]["detail"] == cap.UNSUPPORTED_PROVIDER
    assert declined[0]["payload"]["action"] == "no route change"


# --- Named unavailability survives to the audit trail -------------------


class _Sub:
    plan = "Plus"
    monthly_credits = 20.0
    credits_remaining = 5.0
    current_period_end = None


class _CachedJwtInfo:
    """A Nous portal answer reconstructed from cached JWT claims.

    ``source='jwt'`` / ``fresh=False`` is the real shape the portal returns
    when the live account read could not be made: the claims carry entitlement,
    not current balance, so the numbers beside them describe nothing.
    """

    logged_in = True
    source = "jwt"
    fresh = False
    error = None
    paid_service_access = True
    subscription = _Sub()


@pytest.fixture
def cached_jwt_portal(monkeypatch):
    """Drive the REAL Nous adapter from a cached-JWT portal answer.

    Patched at the portal read rather than at ``fetch_capacity_snapshot`` so
    the whole chain under test is production code: adapter -> snapshot ->
    ``capacity_signal`` -> ``evaluate_throttle`` -> persisted audit row.
    """
    import hermes_cli.nous_account as na

    monkeypatch.setattr(
        na, "get_nous_portal_account_info", lambda **kw: _CachedJwtInfo(),
        raising=True,
    )
    kt.reset_signal_cache()


_NOUS_CFG = {
    "usage_throttle": {
        "source_providers": ["nous"],
        "levers": {"downgrade_model": {
            "enabled": True, "threshold_pct": 1,
            "ladder": ["expensive", "cheap"],
        }},
    }
}


def test_cached_jwt_reading_is_audited_by_name_not_as_generic_unavailability(
    cached_jwt_portal,
):
    """AC4 end-to-end: the enumerated reason survives to the durable record.

    ``stale_portal_reading`` and ``provider_unavailable`` call for different
    operator actions — re-authenticate versus wait for the provider — so
    collapsing the named code into the generic one makes the audit trail
    actively misleading about what is wrong.
    """
    snapshot = cap.nous_capacity_snapshot()
    assert snapshot is not None
    assert snapshot.unavailable_reason == cap.STALE_PORTAL_READING

    signal = kt.capacity_signal("nous", cfg=kt.load_throttle_config(_NOUS_CFG))
    assert signal.fresh is False
    assert signal.used_percent is None
    assert signal.reason == cap.STALE_PORTAL_READING

    decision = kt.evaluate_throttle(kanban_cfg=_NOUS_CFG, now=11_000)
    assert decision.degraded is True
    (payload,) = [
        e["payload"] for e in kt.recent_throttle_events(50)
        if e["kind"] == "degraded"
    ]
    assert payload["provider_detail"] == [f"nous:{cap.STALE_PORTAL_READING}"]


def test_arbitrary_provider_unavailability_stays_generic(cfg, capacity):
    """The other half of the contract: only codes this module mints are
    carried through. Free-form provider text must never reach a durable row.
    """
    capacity(_snapshot(
        provider="nous", unavailable_reason="Account 12345 suspended: contact support",
    ))
    assert kt.capacity_signal("nous", cfg=cfg).reason == "provider_unavailable"


def test_cached_jwt_episode_is_recorded_once_and_mutates_no_route(
    cached_jwt_portal,
):
    """A named degraded reason is still an episode, not a per-tick log, and it
    holds every route exactly as filed."""
    for tick in (12_000, 12_100, 12_200):
        kt.reset_signal_cache()  # force a real re-read, so dedup is proven
        decision = kt.evaluate_throttle(kanban_cfg=_NOUS_CFG, now=tick)
        assert decision.degraded is True
        assert decision.pressure_percent is None
        assert kt.plan_route_change(
            decision, assignee="worker", model="expensive", provider="anthropic",
        ) is None

    degraded = [e for e in kt.recent_throttle_events(50) if e["kind"] == "degraded"]
    assert len(degraded) == 1


# --- Secrecy ------------------------------------------------------------


def test_degraded_audit_records_reason_codes_only():
    """AC5: the audit trail is durable and operator-visible, so a payload that
    echoed a provider response would persist a leak."""
    import re

    kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["xai", "grok"]}},
        now=7_000,
    )
    events = kt.recent_throttle_events(50)
    assert events, "the degraded condition must be recorded at all"
    blob = repr(events)
    assert cap.UNSUPPORTED_PROVIDER in blob  # the classification IS recorded
    assert not re.search(r"(?i)bearer\s+\S|sk-[A-Za-z0-9]|eyJ[A-Za-z0-9_-]{10,}", blob)
    assert not re.search(r"[A-Za-z0-9_-]{40,}", blob)


def test_unsupported_audit_carries_the_evidence_for_the_verdict():
    """AC1: "unsupported" without justification is indistinguishable from a
    bug.  The operator must be able to see the provider WAS probed and serves
    no quota document, from the record alone — not by reading source."""
    kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["xai"]}}, now=7_500,
    )
    payloads = [
        e["payload"] for e in kt.recent_throttle_events(50)
        if e["payload"].get("reason") == kt.DEGRADED_UNSUPPORTED_SOURCE
    ]
    assert payloads, "the unsupported condition must be recorded"
    detail = " ".join(payloads[0]["provider_detail"])
    assert cap.UNSUPPORTED_PROVIDER in detail
    # The evidence is the capability table's own text, carried verbatim, so the
    # record cannot drift from the verdict it justifies.
    assert cap.capability_for("xai").evidence in detail


def test_public_state_of_an_exhausted_account_carries_no_payload(capacity):
    capacity(_snapshot(
        limit_reached=True, allowed=False,
        windows=(AccountUsageWindow(label="Session", used_percent=2.0),),
    ))
    decision = kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["openai-codex"]}},
        now=8_000,
    )
    state = decision.public_state()
    assert state["pressure_percent"] == 100.0
    assert state["drain"] is True
    assert set(state) == {
        "enabled", "state", "previous_state", "changed", "pressure_percent",
        "provider", "window", "degraded", "degraded_reason", "observed_at",
        "max_in_progress", "drain", "downgrade", "operator_intent", "recovery",
    }


# --- Restart / multi-board persistence ----------------------------------


def test_unsupported_degraded_state_survives_a_restart():
    """The state row is shared across board dispatchers and restarts; a
    re-import must not re-emit the same episode."""
    kanban_cfg = {"usage_throttle": {"source_providers": ["xai"]}}
    kt.evaluate_throttle(kanban_cfg=kanban_cfg, now=9_000)
    first = len(kt.recent_throttle_events(50))
    kt.evaluate_throttle(kanban_cfg=kanban_cfg, now=9_600)
    assert len(kt.recent_throttle_events(50)) == first


def test_dry_run_reports_unsupported_without_writing_history():
    """A report of what a tick WOULD do must not manufacture a record."""
    decision = kt.evaluate_throttle(
        kanban_cfg={"usage_throttle": {"source_providers": ["xai"]}},
        now=10_000, persist=False,
    )
    assert decision.degraded_reason == kt.DEGRADED_UNSUPPORTED_SOURCE
    assert kt.recent_throttle_events(50) == []
