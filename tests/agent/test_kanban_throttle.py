"""Behavior contracts for usage-aware Kanban admission (``agent.kanban_throttle``).

Every test drives fake AUTHENTICATED snapshots (the real
:class:`agent.account_usage.AccountUsageSnapshot` shape) through the module's
own fetch seam and controls time explicitly — no live quota is consumed, no
credential is read, and nothing sleeps.
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import kanban_throttle as kt
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow


@pytest.fixture
def throttle_home(tmp_path, monkeypatch):
    """Sandbox the shared kanban home AND assert the sandbox took.

    ``HERMES_KANBAN_HOME`` alone does not isolate a kanban path: the dispatcher
    exports several ``HERMES_KANBAN_*`` overrides into every worker env, and one
    inherited value silently redirects writes at the live board. Clear them all,
    then assert the resolved DB path really is inside the temp dir before any
    test writes a row.
    """
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kt.reset_signal_cache()
    assert kt.throttle_state_db_path().resolve().is_relative_to(tmp_path.resolve())
    yield home
    kt.reset_signal_cache()


def _snapshot(*windows, provider="anthropic", age_seconds=0, unavailable=None):
    """An authenticated-shaped snapshot; ``windows`` are ``(label, pct)`` or
    ``(label, pct, is_active)`` triples."""
    built = []
    for window in windows:
        label, pct = window[0], window[1]
        is_active = window[2] if len(window) > 2 else None
        built.append(
            AccountUsageWindow(label=label, used_percent=pct, is_active=is_active)
        )
    return AccountUsageSnapshot(
        provider=provider, source="test",
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        windows=tuple(built), unavailable_reason=unavailable,
    )


def _serve(monkeypatch, mapping):
    """Point the module's authenticated fetch seam at fixed snapshots."""
    def _fetch(provider, *, timeout):  # noqa: ARG001 - signature parity
        value = mapping.get(provider)
        return value() if callable(value) else value

    monkeypatch.setattr(kt, "_fetch_snapshot", _fetch)
    kt.reset_signal_cache()


def _config(**overrides):
    """Shipped defaults with a usable ladder, plus per-test overrides."""
    levers = {
        "downgrade_model": {"ladder": ["claude-opus-5", "claude-sonnet-5", "gpt-5.6-luna"]},
    }
    for key, value in (overrides.pop("levers", {}) or {}).items():
        levers.setdefault(key, {}).update(value)
    return {"usage_throttle": {"levers": levers, **overrides}}


# --- AC1: worst active window drives a deterministic state with hysteresis ---


def test_worst_active_window_drives_pressure_and_ignores_inactive_windows(
    throttle_home, monkeypatch,
):
    # The 5-hour window is nearly spent but INACTIVE; the weekly window is the
    # worst thing actually counting. Taking the raw max would invent pressure.
    _serve(monkeypatch, {"anthropic": _snapshot(
        ("Five Hour", 99.0, False), ("Seven Day", 72.0, True), ("Opus Week", 40.0, None),
    )})
    cfg = kt.load_throttle_config(_config())
    signal, every = kt.worst_capacity_signal(cfg=cfg)

    assert signal is not None and signal.fresh
    assert signal.used_percent == 72.0
    assert signal.window_label == "Seven Day"
    assert len(every) == 1

    # A window with no explicit is_active still counts — a provider that omits
    # the field must not read as "nothing is active".
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 81.0, None))})
    silent, _ = kt.worst_capacity_signal(cfg=cfg)
    assert silent is not None and silent.used_percent == 81.0


@pytest.mark.parametrize(
    "pressure,expected",
    [
        (10.0, kt.STATE_NORMAL),
        (69.9, kt.STATE_NORMAL),
        (70.0, kt.STATE_REDUCE),
        (79.9, kt.STATE_REDUCE),
        (80.0, kt.STATE_DOWNGRADE),
        (89.9, kt.STATE_DOWNGRADE),
        (90.0, kt.STATE_DRAIN),
        (100.0, kt.STATE_DRAIN),
    ],
)
def test_ladder_is_deterministic_at_every_configured_threshold(pressure, expected):
    cfg = kt.load_throttle_config(_config())
    assert kt.target_state(pressure, cfg) == expected
    assert kt.next_state(kt.STATE_NORMAL, pressure, cfg) == expected


def test_hysteresis_holds_state_between_stepdown_and_resume_thresholds():
    cfg = kt.load_throttle_config(_config())

    # Escalation is immediate.
    assert kt.next_state(kt.STATE_NORMAL, 91.0, cfg) == kt.STATE_DRAIN
    # Falling back below the drain threshold does NOT step down one rung: the
    # state holds until the separate resume threshold is reached, so the board
    # cannot oscillate across a single boundary.
    assert kt.next_state(kt.STATE_DRAIN, 85.0, cfg) == kt.STATE_DRAIN
    assert kt.next_state(kt.STATE_DRAIN, 60.0, cfg) == kt.STATE_DRAIN
    assert kt.next_state(kt.STATE_DRAIN, 51.0, cfg) == kt.STATE_DRAIN
    # At or below resume.threshold_pct it recovers all the way to normal.
    assert kt.next_state(kt.STATE_DRAIN, 50.0, cfg) == kt.STATE_NORMAL
    assert kt.next_state(kt.STATE_REDUCE, 49.0, cfg) == kt.STATE_NORMAL


def test_resume_threshold_is_independent_of_the_stepdown_thresholds():
    cfg = kt.load_throttle_config(_config(resume={"threshold_pct": 20}))
    assert cfg.resume_threshold_pct == 20.0
    assert kt.next_state(kt.STATE_REDUCE, 50.0, cfg) == kt.STATE_REDUCE
    assert kt.next_state(kt.STATE_REDUCE, 20.0, cfg) == kt.STATE_NORMAL


# --- AC2: configurable levers that never mutate healthy operator settings ---


def test_each_lever_toggles_independently_and_releases_its_own_state():
    cfg = kt.load_throttle_config(
        _config(levers={"pause_drain": {"enabled": False}})
    )
    # Drain off: maximum pressure escalates only as far as the next lever down.
    assert kt.target_state(99.0, cfg) == kt.STATE_DOWNGRADE
    # A board already draining when the operator disables the lever releases it
    # at once rather than staying stuck in a state nothing can now produce.
    assert kt.next_state(kt.STATE_DRAIN, 99.0, cfg) == kt.STATE_DOWNGRADE

    only_drain = kt.load_throttle_config(_config(levers={
        "reduce_concurrency": {"enabled": False},
        "downgrade_model": {"enabled": False},
    }))
    assert kt.target_state(75.0, only_drain) == kt.STATE_NORMAL
    assert kt.target_state(95.0, only_drain) == kt.STATE_DRAIN


def test_thresholds_and_concurrency_ceiling_are_configurable():
    cfg = kt.load_throttle_config(_config(levers={
        "reduce_concurrency": {"threshold_pct": 35, "max_in_progress": 1},
        "downgrade_model": {"threshold_pct": 55},
        "pause_drain": {"threshold_pct": 65},
    }))
    assert kt.target_state(40.0, cfg) == kt.STATE_REDUCE
    assert kt.target_state(60.0, cfg) == kt.STATE_DOWNGRADE
    assert kt.target_state(70.0, cfg) == kt.STATE_DRAIN
    assert cfg.reduce.max_in_progress == 1


def test_concurrency_ceiling_only_ever_narrows_the_operator_value(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 75.0))})
    decision = kt.evaluate_throttle(
        kanban_cfg=_config(levers={"reduce_concurrency": {"max_in_progress": 4}}),
        operator_max_in_progress=8,
    )
    assert decision.state == kt.STATE_REDUCE
    # Operator's 8 is narrowed to the automatic 4 ...
    assert decision.narrowed_max_in_progress(8) == 4
    # ... but an operator who already runs tighter than the automatic ceiling
    # keeps their own value: the throttle never widens anything.
    assert decision.narrowed_max_in_progress(2) == 2
    # An uncapped board gains the automatic ceiling rather than staying uncapped.
    assert decision.narrowed_max_in_progress(None) == 4


def test_throttle_never_writes_operator_configuration(throttle_home, monkeypatch):
    config_path = throttle_home / "config.yaml"
    config_path.write_text("kanban:\n  max_in_progress: 6\n", encoding="utf-8")
    before = config_path.read_bytes()

    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 95.0))})
    decision = kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=6)

    assert decision.state == kt.STATE_DRAIN
    # The automatic value lives in the decision, not in the operator's file.
    assert config_path.read_bytes() == before


# --- AC3: auditable, deduplicated transitions and degraded conditions ---


def test_every_transition_is_recorded_once_even_across_concurrent_boards(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 92.0))})
    first = kt.evaluate_throttle(kanban_cfg=_config(), board="alpha", now=1_000)
    assert (first.state, first.changed) == (kt.STATE_DRAIN, True)

    # A second board ticking on the same observation must not double-record the
    # one transition, and must see the state the first board established.
    second = kt.evaluate_throttle(kanban_cfg=_config(), board="beta", now=1_000)
    assert (second.state, second.changed) == (kt.STATE_DRAIN, False)

    changes = [e for e in kt.recent_throttle_events(50) if e["kind"] == "state_change"]
    assert len(changes) == 1
    assert changes[0]["payload"]["from"] == kt.STATE_NORMAL
    assert changes[0]["payload"]["to"] == kt.STATE_DRAIN
    assert changes[0]["payload"]["pressure_percent"] == 92.0


def test_audit_payloads_carry_no_credential_or_raw_account_data(
    throttle_home, monkeypatch,
):
    secret = "sk-ant-oat01-do-not-log"
    snapshot = AccountUsageSnapshot(
        provider="anthropic", source="oauth_usage_api",
        fetched_at=datetime.now(timezone.utc), plan=secret,
        windows=(AccountUsageWindow(label="Seven Day", used_percent=93.0),),
        details=(f"token={secret}",),
    )
    _serve(monkeypatch, {"anthropic": snapshot})
    decision = kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=4)

    rendered = repr(kt.recent_throttle_events(50)) + repr(decision.public_state())
    assert secret not in rendered
    assert "token=" not in rendered


def test_degraded_condition_is_visible_deduplicated_and_re_armed_per_episode(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 95.0))})
    kt.evaluate_throttle(kanban_cfg=_config(), now=1_000)

    # Signal disappears: three ticks, ONE degraded record for the episode.
    _serve(monkeypatch, {"anthropic": None})
    for tick in (1_100, 1_200, 1_300):
        degraded = kt.evaluate_throttle(kanban_cfg=_config(), now=tick)
        assert degraded.degraded is True
        assert degraded.degraded_reason == kt.DEGRADED_NO_SIGNAL
        # Holding the last known state: no speculative change in EITHER
        # direction. Recovering here would invent headroom.
        assert degraded.state == kt.STATE_DRAIN
        assert degraded.changed is False

    events = kt.recent_throttle_events(50)
    assert len([e for e in events if e["kind"] == "degraded"]) == 1
    assert events[0]["payload"]["recovery"]

    # A healthy reading ends the episode; a LATER outage is a new episode and
    # records again rather than being silently suppressed forever.
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 10.0))})
    kt.evaluate_throttle(kanban_cfg=_config(), now=1_400)
    _serve(monkeypatch, {"anthropic": None})
    kt.evaluate_throttle(kanban_cfg=_config(), now=1_500)
    assert len([e for e in kt.recent_throttle_events(50) if e["kind"] == "degraded"]) == 2


@pytest.mark.parametrize(
    "snapshot,reason",
    [
        (None, "fetch_unavailable"),
        (_snapshot(("Seven Day", 95.0), unavailable="Not an OAuth account."),
         "provider_unavailable"),
        (_snapshot(("Seven Day", 95.0), age_seconds=100_000), "stale"),
        (_snapshot(), "no_active_window"),
        (_snapshot(("Seven Day", 95.0, False)), "no_active_window"),
    ],
)
def test_every_unusable_signal_shape_is_classified_and_never_guessed(
    throttle_home, monkeypatch, snapshot, reason,
):
    _serve(monkeypatch, {"anthropic": snapshot})
    cfg = kt.load_throttle_config(_config())
    signal = kt.capacity_signal("anthropic", cfg=cfg)
    assert signal.fresh is False
    assert signal.reason == reason
    assert signal.used_percent is None


def test_error_from_the_provider_fetch_is_degraded_not_a_crash(
    throttle_home, monkeypatch,
):
    def _boom(provider, *, timeout):  # noqa: ARG001
        raise RuntimeError("connection reset")

    monkeypatch.setattr(kt, "_fetch_snapshot", _boom)
    kt.reset_signal_cache()
    decision = kt.evaluate_throttle(kanban_cfg=_config())
    assert decision.degraded is True
    assert decision.state == kt.STATE_NORMAL


# --- AC6: automatic, visible recovery restoring exact operator intent ---


def test_recovery_is_automatic_visible_and_restores_exact_operator_intent(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 20.0))})
    healthy = kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=7, now=10)
    assert healthy.state == kt.STATE_NORMAL
    assert healthy.narrowed_max_in_progress(7) == 7

    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 96.0))})
    throttled = kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=7, now=20)
    assert throttled.drain is True
    assert throttled.narrowed_max_in_progress(7) == 2

    # Usage resets. No operator action, no restore step — the automatic value
    # simply stops applying and the operator's own cap is live again.
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 5.0))})
    recovered = kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=7, now=30)
    assert recovered.state == kt.STATE_NORMAL
    assert recovered.changed is True
    assert recovered.drain is False
    assert recovered.max_in_progress is None
    assert recovered.narrowed_max_in_progress(7) == 7

    recovery = [
        e for e in kt.recent_throttle_events(50)
        if e["kind"] == "state_change" and e["payload"]["to"] == kt.STATE_NORMAL
    ]
    assert len(recovery) == 1
    assert recovery[0]["payload"]["restored_operator_intent"] == {"max_in_progress": 7}


def test_state_and_intent_survive_a_process_restart(throttle_home, monkeypatch):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 20.0))})
    kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=5, now=10)
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 93.0))})
    kt.evaluate_throttle(kanban_cfg=_config(), operator_max_in_progress=5, now=20)

    # Re-import the module: every in-process cache is gone, exactly as after a
    # dispatcher restart. The state must come back from durable storage.
    reloaded = importlib.reload(kt)
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 85.0))})
    monkeypatch.setattr(reloaded, "_fetch_snapshot", kt._fetch_snapshot)
    reloaded.reset_signal_cache()

    after = reloaded.evaluate_throttle(
        kanban_cfg=_config(), operator_max_in_progress=5, now=30,
    )
    # 85% is below the drain threshold but above the resume threshold, so a
    # process that had forgotten the state would step DOWN to downgrade_model.
    # Persistence is what keeps the drain in force across the restart.
    assert after.state == reloaded.STATE_DRAIN
    assert after.operator_intent == {"max_in_progress": 5}
    importlib.reload(kt)


# --- Model downgrade ladder ---------------------------------------------


def test_downgrade_walks_one_rung_and_leaves_unknown_models_alone(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 82.0))})
    decision = kt.evaluate_throttle(kanban_cfg=_config())
    assert decision.state == kt.STATE_DOWNGRADE

    step = kt.plan_route_change(
        decision, assignee="worker", model="claude-opus-5", provider="anthropic",
    )
    assert step is not None
    assert (step.kind, step.model, step.provider) == (
        "downgrade", "claude-sonnet-5", "anthropic",
    )

    # A model that is not on the ladder is left alone rather than guessed at.
    assert kt.plan_route_change(
        decision, assignee="worker", model="some-other-model", provider="anthropic",
    ) is None
    # The cheapest rung has nowhere further to go.
    assert kt.plan_route_change(
        decision, assignee="worker", model="gpt-5.6-luna", provider="anthropic",
    ) is None


def test_no_route_change_below_the_downgrade_threshold_or_while_degraded(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 72.0))})
    reduce_only = kt.evaluate_throttle(kanban_cfg=_config())
    assert reduce_only.state == kt.STATE_REDUCE
    assert kt.plan_route_change(
        reduce_only, assignee="worker", model="claude-opus-5", provider="anthropic",
    ) is None

    _serve(monkeypatch, {"anthropic": None})
    degraded = kt.evaluate_throttle(kanban_cfg=_config())
    assert degraded.degraded is True
    assert kt.plan_route_change(
        degraded, assignee="worker", model="claude-opus-5", provider="anthropic",
    ) is None


# --- AC5: cross-provider failover is opt-in and dual-signal gated ---------


def _failover_config(**destination_overrides):
    destination = {
        "provider": "openai-codex", "model": "gpt-5.6-sol", "max_pressure_pct": 50,
        **destination_overrides,
    }
    return _config(levers={"cross_provider_failover": {
        "enabled": True, "threshold_pct": 95, "eligible_profiles": ["overflow-worker"],
        "destination": destination,
    }})


def test_failover_is_off_by_default_even_at_maximum_pressure(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 100.0)),
        "openai-codex": _snapshot(("Session", 1.0), provider="openai-codex"),
    })
    shipped = kt.load_throttle_config(_config())
    assert shipped.failover.enabled is False
    assert shipped.failover.eligible_profiles == ()

    decision = kt.evaluate_throttle(kanban_cfg=_config())
    assert decision.state == kt.STATE_DRAIN
    assert kt.plan_failover(decision, assignee="overflow-worker") is None


def test_failover_eligibility_is_empty_unless_every_gate_is_proven(
    throttle_home, monkeypatch,
):
    # This helper is what lets a drain still admit rerouted work, so an
    # over-permissive answer would defeat the drain itself. It must be empty on
    # the shipped defaults and on every unproven case.
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 99.0)),
        "openai-codex": _snapshot(("Session", 5.0), provider="openai-codex"),
    })
    shipped = kt.evaluate_throttle(kanban_cfg=_config())
    assert kt.failover_eligible_profiles(shipped) == ()

    armed = kt.evaluate_throttle(kanban_cfg=_failover_config())
    assert kt.failover_eligible_profiles(armed) == ("overflow-worker",)

    # Destination goes dark: eligibility collapses to empty, so a draining
    # board admits nobody rather than everybody.
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 99.0)), "openai-codex": None,
    })
    blind = kt.evaluate_throttle(kanban_cfg=_failover_config())
    assert kt.failover_eligible_profiles(blind) == ()


def test_failover_requires_the_profile_to_be_on_the_explicit_allowlist(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 99.0)),
        "openai-codex": _snapshot(("Session", 5.0), provider="openai-codex"),
    })
    decision = kt.evaluate_throttle(kanban_cfg=_failover_config())

    # The named profile is eligible ...
    allowed = kt.plan_failover(decision, assignee="overflow-worker")
    assert allowed is not None
    assert (allowed.kind, allowed.provider, allowed.model) == (
        "failover", "openai-codex", "gpt-5.6-sol",
    )
    # ... and nobody else is, however plausible. Eligibility is never inferred
    # from a profile merely having that provider configured.
    assert kt.plan_failover(decision, assignee="other-worker") is None
    assert kt.plan_failover(decision, assignee=None) is None


@pytest.mark.parametrize(
    "destination_snapshot,label",
    [
        (None, "no destination signal at all"),
        (_snapshot(("Session", 5.0), provider="openai-codex", age_seconds=100_000),
         "stale destination signal"),
        (_snapshot(provider="openai-codex"), "destination reports no active window"),
        (_snapshot(("Session", 80.0), provider="openai-codex"),
         "destination is itself under pressure"),
    ],
)
def test_failover_refuses_without_fresh_destination_capacity(
    throttle_home, monkeypatch, destination_snapshot, label,
):
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 99.0)),
        "openai-codex": destination_snapshot,
    })
    decision = kt.evaluate_throttle(kanban_cfg=_failover_config())
    assert decision.state == kt.STATE_DRAIN

    assert kt.plan_failover(decision, assignee="overflow-worker") is None, label
    # The refusal is visible, not silent.
    degraded = [
        e for e in kt.recent_throttle_events(50)
        if e["kind"] == "degraded"
        and e["payload"]["reason"] == kt.DEGRADED_FAILOVER_DESTINATION
    ]
    assert degraded, label
    assert degraded[0]["payload"]["action"] == "no route change"


def test_failover_refusal_is_audited_once_per_episode_not_once_per_database(
    throttle_home, monkeypatch,
):
    # Deduplication must scope to the degraded EPISODE, exactly as the
    # no-capacity-signal path does. A destination account that flaps refuses
    # reroutes over and over, and the audit row (with its warning) is the only
    # signal an operator gets that it is happening; collapsing every later
    # refusal into the first record makes a recurring refusal invisible.
    def _refusals():
        return [
            e for e in kt.recent_throttle_events(50)
            if e["kind"] == "degraded"
            and e["payload"]["reason"] == kt.DEGRADED_FAILOVER_DESTINATION
        ]

    cfg = _failover_config()
    source = _snapshot(("Seven Day", 99.0))
    healthy_destination = _snapshot(("Session", 5.0), provider="openai-codex")

    # Episode 1 — the destination is unreadable, so the reroute is refused.
    _serve(monkeypatch, {"anthropic": source, "openai-codex": None})
    blind = kt.evaluate_throttle(kanban_cfg=cfg, now=1_000)
    assert kt.failover_eligible_profiles(blind, now=1_000) == ()
    assert len(_refusals()) == 1

    # The destination proves itself again and the lever arms: episode over.
    _serve(monkeypatch, {"anthropic": source, "openai-codex": healthy_destination})
    armed = kt.evaluate_throttle(kanban_cfg=cfg, now=2_000)
    assert kt.failover_eligible_profiles(armed, now=2_000) == ("overflow-worker",)

    # Episode 2 — it goes dark again. A refusal after an intervening healthy
    # observation is a new episode and earns its own record.
    _serve(monkeypatch, {"anthropic": source, "openai-codex": None})
    again = kt.evaluate_throttle(kanban_cfg=cfg, now=3_000)
    assert kt.failover_eligible_profiles(again, now=3_000) == ()
    assert len(_refusals()) == 2

    # Within one episode the refusal still records exactly once, however many
    # times it is consulted — two boards and every queued card ask this same
    # question each tick.
    for _ in range(4):
        assert kt.plan_failover(again, assignee="overflow-worker", now=3_000) is None
    assert len(_refusals()) == 2

    # Two episodes can open and close inside a single second, so the episode
    # marker cannot be the clock: a timestamped fingerprint would collide here
    # and silently drop the second refusal.
    _serve(monkeypatch, {"anthropic": source, "openai-codex": healthy_destination})
    kt.failover_eligible_profiles(
        kt.evaluate_throttle(kanban_cfg=cfg, now=4_000), now=4_000
    )
    _serve(monkeypatch, {"anthropic": source, "openai-codex": None})
    kt.failover_eligible_profiles(
        kt.evaluate_throttle(kanban_cfg=cfg, now=4_000), now=4_000
    )
    assert len(_refusals()) == 3


def test_failover_refuses_when_the_source_signal_is_not_fresh(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 99.0)),
        "openai-codex": _snapshot(("Session", 2.0), provider="openai-codex"),
    })
    kt.evaluate_throttle(kanban_cfg=_failover_config(), now=1_000)

    # Source goes dark while the destination stays healthy: an unknown source
    # is not evidence of pressure, so no reroute may be planned off it.
    _serve(monkeypatch, {
        "anthropic": None,
        "openai-codex": _snapshot(("Session", 2.0), provider="openai-codex"),
    })
    blind = kt.evaluate_throttle(kanban_cfg=_failover_config(), now=1_100)
    assert blind.degraded is True
    assert kt.plan_failover(blind, assignee="overflow-worker") is None


def test_failover_requires_a_configured_destination_route(throttle_home, monkeypatch):
    _serve(monkeypatch, {"anthropic": _snapshot(("Seven Day", 99.0))})
    decision = kt.evaluate_throttle(kanban_cfg=_failover_config(provider=""))
    assert kt.plan_failover(decision, assignee="overflow-worker") is None


def test_failover_requires_source_pressure_at_its_own_threshold(
    throttle_home, monkeypatch,
):
    _serve(monkeypatch, {
        "anthropic": _snapshot(("Seven Day", 91.0)),
        "openai-codex": _snapshot(("Session", 1.0), provider="openai-codex"),
    })
    decision = kt.evaluate_throttle(kanban_cfg=_failover_config())
    # Draining already, but below the failover threshold of 95: draining is
    # the correct response, not moving work onto another account.
    assert decision.state == kt.STATE_DRAIN
    assert kt.plan_failover(decision, assignee="overflow-worker") is None


# --- Master switch and config robustness ---------------------------------


def test_disabled_throttle_is_completely_inert(throttle_home, monkeypatch):
    def _never(provider, *, timeout):  # noqa: ARG001
        raise AssertionError("a disabled throttle must not contact the provider")

    monkeypatch.setattr(kt, "_fetch_snapshot", _never)
    decision = kt.evaluate_throttle(kanban_cfg=_config(enabled=False))

    assert decision.enabled is False
    assert decision.drain is False
    assert decision.max_in_progress is None
    assert decision.narrowed_max_in_progress(9) == 9
    assert not kt.throttle_state_db_path().exists()


def test_malformed_config_values_fall_back_without_raising():
    cfg = kt.load_throttle_config({"usage_throttle": {
        "enabled": "yes",
        "signal_max_age_seconds": -5,
        "poll_interval_seconds": "soon",
        "source_providers": "anthropic",
        "levers": {
            "reduce_concurrency": {"threshold_pct": 900, "max_in_progress": 0},
            "downgrade_model": {"ladder": "claude-opus-5"},
            "pause_drain": {"threshold_pct": None},
            "cross_provider_failover": {"eligible_profiles": {"bad": "shape"}},
        },
        "resume": {"threshold_pct": -40},
    }})
    assert cfg.enabled is True                    # non-bool -> shipped default
    assert cfg.signal_max_age_seconds == 900      # out of range -> default
    assert cfg.poll_interval_seconds == 120
    assert cfg.source_providers == ("anthropic",)  # bare string is accepted
    assert cfg.reduce.threshold_pct == 100.0       # clamped, not rejected
    assert cfg.reduce.max_in_progress == 2
    assert cfg.downgrade.ladder == ("claude-opus-5",)
    assert cfg.drain.threshold_pct == 90.0
    assert cfg.failover.eligible_profiles == ()
    assert cfg.resume_threshold_pct == 0.0


def test_shipped_defaults_match_the_documented_posture():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    shipped = DEFAULT_CONFIG["kanban"]["usage_throttle"]
    cfg = kt.load_throttle_config({"usage_throttle": shipped})
    # All three local levers ship enabled (the parent card's FINAL decision) ...
    assert (cfg.enabled, cfg.reduce.enabled, cfg.downgrade.enabled, cfg.drain.enabled) == (
        True, True, True, True,
    )
    # ... and cross-provider failover ships OFF with an empty allowlist and no
    # destination (the FINAL audit decision).
    assert cfg.failover.enabled is False
    assert cfg.failover.eligible_profiles == ()
    assert cfg.failover.destination_provider is None
    # Thresholds are ordered so each lever arms strictly after the previous one,
    # and the resume point sits below all of them.
    assert (
        cfg.resume_threshold_pct
        < cfg.reduce.threshold_pct
        < cfg.downgrade.threshold_pct
        < cfg.drain.threshold_pct
        < cfg.failover.threshold_pct
    )


def test_capacity_reads_are_cached_so_boards_do_not_multiply_provider_calls(
    throttle_home, monkeypatch,
):
    calls: list[str] = []

    def _counting(provider, *, timeout):  # noqa: ARG001
        calls.append(provider)
        return _snapshot(("Seven Day", 30.0))

    monkeypatch.setattr(kt, "_fetch_snapshot", _counting)
    kt.reset_signal_cache()
    cfg = kt.load_throttle_config(_config(poll_interval_seconds=300))

    for _ in range(5):
        kt.capacity_signal("anthropic", cfg=cfg, now=1_000)
    assert len(calls) == 1

    # Past the poll interval the reading is refreshed rather than served forever.
    kt.capacity_signal("anthropic", cfg=cfg, now=1_000 + 301)
    assert len(calls) == 2
