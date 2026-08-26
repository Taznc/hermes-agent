from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent.account_usage import AccountUsageSnapshot
from hermes_cli import web_server


def test_account_limits_route_uses_worker_and_serializes_safe_accounts(monkeypatch):
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        unavailable_reason="No compatible account credentials are configured for provider limits.",
    )
    calls = []

    def fetch(providers):
        calls.append(("fetch", providers))
        return (snapshot,)

    async def to_thread(fn, *args):
        calls.append(("to_thread", fn, args))
        return fn(*args)

    monkeypatch.setattr("agent.account_usage.fetch_account_limits", fetch)
    monkeypatch.setattr(web_server.asyncio, "to_thread", to_thread)

    payload = asyncio.run(web_server.get_account_limits(providers="openai-codex"))

    assert calls[0][0] == "to_thread"
    assert calls[1] == ("fetch", ("openai-codex",))
    assert payload["accounts"][0]["provider"] == "openai-codex"
    assert payload["accounts"][0]["available"] is False
    assert "email" not in repr(payload)
    assert "account_id" not in repr(payload)


def test_account_limits_route_rejects_unknown_provider():
    try:
        asyncio.run(web_server.get_account_limits(providers="openai"))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    else:
        raise AssertionError("expected an HTTP 422 for unsupported provider")
