"""Anthropic ``oauth/usage`` utilization is a percent: 1.0 means 1%, never 100%."""

import pytest

from agent import account_usage
from agent.account_usage import fetch_account_usage


def _anthropic(monkeypatch, payload):
    monkeypatch.setattr(account_usage, "_get_json", lambda url, headers, *, timeout: payload)
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda **kw: "sk-ant-oat01-test")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda token: True)


@pytest.mark.parametrize("raw", [0.0, 0.3, 1.0, 1.5, 4.0, 69.0, 100.0])
def test_anthropic_utilization_is_reported_unscaled(monkeypatch, raw):
    _anthropic(monkeypatch, {"five_hour": {"utilization": raw, "resets_at": "2026-10-24T20:10:00Z"},
                             "seven_day": {"utilization": raw, "resets_at": "2026-10-26T14:00:00Z"}})

    snapshot = fetch_account_usage("anthropic")
    assert snapshot is not None

    assert {w.label: w.used_percent for w in snapshot.windows} == {"Current session": raw, "Current week": raw}


def test_one_percent_session_is_not_exhausted(monkeypatch):
    """The reported symptom: a 1% session rendered as a full 100% bar."""
    _anthropic(monkeypatch, {"five_hour": {"utilization": 1.0, "resets_at": "2026-10-24T20:10:00Z"},
                             "seven_day_opus": {"utilization": 1.0, "resets_at": "2026-10-26T14:00:00Z"}})

    snapshot = fetch_account_usage("anthropic")
    assert snapshot is not None
    percents = {w.label: w.used_percent for w in snapshot.windows}

    assert percents == {"Current session": 1.0, "Opus week": 1.0}


def test_other_fraction_callers_still_scale():
    """Only the Anthropic ``utilization`` field is exempt; an explicit fraction elsewhere still scales."""
    windows = account_usage._usage_windows({"w": {"used": 0.25}}, (("w", "W"),), "used", "reset", fraction=True)

    assert windows[0].used_percent == 25.0


def test_explicit_api_key_and_model_scopes_survive(monkeypatch):
    """Carries the non-percent assertions of the superseded upstream test
    ``test_anthropic_fetch_honors_explicit_api_key_and_marks_model_scoped_windows``."""
    called: dict = {}

    def fake_get_json(url, headers, *, timeout):
        called["headers"] = headers
        return {"five_hour": {"utilization": 30.0, "resets_at": "2026-10-05T12:00:00Z"},
                "seven_day": {"utilization": 20.0, "resets_at": "2026-10-08T12:00:00Z"},
                "seven_day_opus": {"utilization": 100.0, "resets_at": "2026-10-09T12:00:00Z"},
                "seven_day_sonnet": {"utilization": 0.4, "resets_at": "2026-10-09T12:00:00Z"}}

    monkeypatch.setattr(account_usage, "_get_json", fake_get_json)
    monkeypatch.setattr(account_usage, "resolve_anthropic_token",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("ambient token must not be consulted")))

    snapshot = fetch_account_usage("anthropic", api_key="sk-ant-oat01-explicit")
    assert snapshot is not None

    assert called["headers"]["Authorization"] == "Bearer sk-ant-oat01-explicit"
    assert {w.label: (w.scope, w.used_percent) for w in snapshot.windows} == {
        "Current session": ("account", 30.0), "Current week": ("account", 20.0),
        "Opus week": ("model", 100.0), "Sonnet week": ("model", 0.4)}
