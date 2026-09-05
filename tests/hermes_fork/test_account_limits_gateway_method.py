from __future__ import annotations

from datetime import datetime, timezone

from agent.account_usage import AccountUsageSnapshot
import tui_gateway.server as srv


def test_account_limits_gateway_method_returns_sanitized_accounts(monkeypatch):
    snapshot = AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        unavailable_reason="No compatible account credentials are configured for provider limits.",
    )
    calls = []

    def fetch(providers):
        calls.append(providers)
        return (snapshot,)

    monkeypatch.setattr("agent.account_usage.fetch_account_limits", fetch)

    envelope = srv._methods["account_limits.get"](1, {"providers": ["anthropic"]})

    assert envelope["result"]["accounts"][0]["provider"] == "anthropic"
    assert calls == [("anthropic",)]
    assert "email" not in repr(envelope)
    assert "account_id" not in repr(envelope)


def test_account_limits_gateway_method_rejects_invalid_provider():
    envelope = srv._methods["account_limits.get"](1, {"providers": ["openai"]})

    assert envelope["error"]["code"] == 4000
