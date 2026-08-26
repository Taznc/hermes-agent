from types import SimpleNamespace

import pytest

from agent import account_usage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, payload):
        self.calls = calls
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payload)


@pytest.fixture
def codex_usage_payload():
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 21,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 4,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }


def test_codex_usage_prefers_explicit_live_agent_credentials(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy auth should not be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert snapshot.provider == "openai-codex"
    assert snapshot.plan == "Plus"
    assert [w.label for w in snapshot.windows] == ["Session", "Weekly"]
    assert snapshot.windows[0].used_percent == 21
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer live-agent-token"


def test_codex_usage_falls_back_to_native_credential_pool(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    # Pool fallback fires only on AuthError (the documented "no creds" mode of
    # the resolver), NOT on arbitrary exceptions — see the transient-error guard
    # test below.
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(
            account_usage.AuthError("no singleton auth", provider="openai-codex", code="codex_auth_missing")
        ),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[1].label == "Weekly"
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer pooled-token"
    # Pool creds have no account_id concept — the ChatGPT-Account-Id header must
    # be omitted rather than sent stale/wrong.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]




def test_codex_usage_account_id_read_failure_keeps_singleton_token(monkeypatch, codex_usage_payload):
    """When the resolver succeeds but the separate account_id read raises, the
    working singleton token must still be used (best-effort account_id), NOT
    abandoned in favor of a header-less pool credential."""
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: {
            "api_key": "singleton-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        account_usage,
        "_read_codex_tokens",
        lambda *a, **k: (_ for _ in ()).throw(
            account_usage.AuthError("partial store", provider="openai-codex", code="codex_auth_invalid_shape")
        ),
    )

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(
        credential_pool,
        "load_pool",
        lambda provider: (_ for _ in ()).throw(AssertionError("pool must not be consulted")),
    )

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert calls[0]["headers"]["Authorization"] == "Bearer singleton-token"
    # account_id read failed → header omitted, but the singleton token is kept.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]




# ── Banked rate-limit reset credits (`/usage reset`) ─────────────────────────


class _FakeResetClient:
    """GET returns the usage payload; POST returns the consume payload."""

    def __init__(self, calls, usage_payload, consume_payload=None):
        self.calls = calls
        self.usage_payload = usage_payload
        self.consume_payload = consume_payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return _FakeResponse(self.usage_payload)

    def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _FakeResponse(self.consume_payload)


def _usage_payload_with_resets(primary_used, secondary_used, banked):
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": primary_used, "reset_at": 1779846359},
            "secondary_window": {"used_percent": secondary_used, "reset_at": 1780230796},
        },
        "rate_limit_reset_credits": {"available_count": banked},
        "credits": {"has_credits": False},
    }
















def test_redeem_missing_credentials_reports_unavailable(monkeypatch):
    monkeypatch.setattr(
        account_usage,
        "_resolve_codex_usage_credentials",
        lambda base_url, api_key: (_ for _ in ()).throw(RuntimeError("no creds")),
    )

    result = account_usage.redeem_codex_reset_credit()

    assert result.status == "unavailable"
    assert "hermes auth" in result.message


def test_codex_usage_keeps_blocked_state_window_metadata_and_safe_balance(monkeypatch):
    payload = {
        "plan_type": "plus",
        "email": "private@example.com",
        "account_id": "acct_private",
        "user_id": "user_private",
        "rate_limit": {
            "allowed": False,
            "limit_reached": True,
            "primary_window": {
                "used_percent": 100,
                "reset_at": 1_900_000_000,
                "limit_window_seconds": 18_000,
            },
            "secondary_window": None,
        },
        "code_review_rate_limit": {
            "used_percent": 64,
            "reset_at": 1_900_010_000,
            "limit_window_seconds": 604_800,
        },
        "credits": {"has_credits": True, "balance": 12.5},
    }
    monkeypatch.setattr(account_usage, "resolve_codex_runtime_credentials", lambda **kwargs: {
        "api_key": "access-token", "base_url": "https://chatgpt.com/backend-api/codex"
    })
    monkeypatch.setattr(account_usage, "_read_codex_tokens", lambda: {"tokens": {}})
    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: _FakeClient([], payload))

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.allowed is False
    assert snapshot.limit_reached is True
    assert snapshot.windows[0].limit_window_seconds == 18_000
    assert snapshot.windows[0].limit_reached is True
    assert snapshot.windows[1].label == "Code review"
    assert snapshot.windows[1].limit_window_seconds == 604_800
    assert snapshot.balances[0].label == "Credits"
    assert snapshot.balances[0].remaining == 12.5
    serialized = account_usage.serialize_account_usage(snapshot)
    assert serialized["limit_reached"] is True
    assert serialized["windows"][0]["limit_window_seconds"] == 18_000
    assert serialized["balances"] == [{"label": "Credits", "remaining": 12.5, "currency": "USD"}]
    assert "private@example.com" not in repr(serialized)
    assert "acct_private" not in repr(serialized)
    assert "user_private" not in repr(serialized)


def test_codex_usage_supports_nested_code_review_windows(monkeypatch):
    payload = {
        "rate_limit": {},
        "code_review_rate_limit": {
            "primary_window": {"used_percent": 12, "reset_at": 1_900_000_000},
            "secondary_window": {"used_percent": 55, "reset_at": 1_900_500_000},
        },
    }
    monkeypatch.setattr(account_usage, "resolve_codex_runtime_credentials", lambda **kwargs: {
        "api_key": "access-token", "base_url": "https://chatgpt.com/backend-api/codex"
    })
    monkeypatch.setattr(account_usage, "_read_codex_tokens", lambda: {"tokens": {}})
    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: _FakeClient([], payload))

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert [(window.label, window.used_percent) for window in snapshot.windows] == [
        ("Code review session", 12.0),
        ("Code review week", 55.0),
    ]


def test_anthropic_usage_maps_self_describing_limits_without_known_kind(monkeypatch):
    payload = {
        "limits": [
            {
                "kind": "tangelo",
                "group": "seven_day",
                "percent": 42,
                "severity": "warning",
                "is_active": True,
                "resets_at": "2030-01-01T00:00:00Z",
                "scope": {"model": "claude-opus-4"},
            }
        ],
        "extra_usage": {"is_enabled": True, "used_credits": 3.5, "monthly_limit": 10, "currency": "USD"},
    }
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-token")
    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: _FakeClient([], payload))

    snapshot = account_usage.fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.windows[0].label == "Tangelo"
    assert snapshot.windows[0].severity == "warning"
    assert snapshot.windows[0].is_active is True
    assert snapshot.windows[0].scope == "claude-opus-4"
    assert snapshot.balances[0].label == "Extra usage"
    assert snapshot.balances[0].used == 3.5
    assert snapshot.balances[0].limit == 10.0


def test_anthropic_usage_keeps_unknown_top_level_window(monkeypatch):
    payload = {
        "tangelo": {"utilization": 0.37, "resets_at": "2030-01-01T00:00:00Z"},
        "future_provider_window": {"utilization": 88, "resets_at": "2030-01-02T00:00:00Z"},
    }
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-token")
    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: _FakeClient([], payload))

    snapshot = account_usage.fetch_account_usage("anthropic")

    assert snapshot is not None
    assert [(window.label, window.used_percent) for window in snapshot.windows] == [
        ("Tangelo", 37.0),
        ("Future Provider Window", 88.0),
    ]


def test_fetch_account_limits_returns_a_sanitized_unavailable_snapshot(monkeypatch):
    monkeypatch.setattr(account_usage, "fetch_account_usage", lambda provider: None)

    snapshots = account_usage.fetch_account_limits(("anthropic", "openai-codex"))

    assert [snapshot.provider for snapshot in snapshots] == ["anthropic", "openai-codex"]
    assert all(snapshot.available is False for snapshot in snapshots)
    assert all("No compatible" in (snapshot.unavailable_reason or "") for snapshot in snapshots)
