from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent.account_usage import AccountUsageSnapshot
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_fork.model_recommendation import service
import tui_gateway.server as srv


CANDIDATES = [
    {
        "provider": "anthropic",
        "model": "claude-fast",
        "capabilities": {"reasoning": True, "fast": True, "effort_options": list(service.EFFORTS)},
        "cost": "free",
    },
    {
        "provider": "openai-codex",
        "model": "codex-strong",
        "capabilities": {"reasoning": True, "fast": False, "effort_options": list(service.EFFORTS)},
        "cost": "paid_or_unknown",
    },
]


def _router_config():
    return {"auxiliary": {"model_recommendation": {"provider": "anthropic", "model": "router", "timeout": 20}}}


def _router_output(*, ambiguous: bool = False):
    return (
        '{"task_risk":"medium","ambiguous":%s,"recommendations":['
        '{"provider":"anthropic","model":"claude-fast","effort":"medium","reason":"Adequate","quality":60,"materially_advantageous":false},'
        '{"provider":"openai-codex","model":"codex-strong","effort":"high","reason":"Strongest","quality":95,"materially_advantageous":false}]}'
    ) % ("true" if ambiguous else "false")


def _configure_router(monkeypatch, *, output: str | None = None):
    monkeypatch.setattr("hermes_cli.config.load_config", _router_config)
    monkeypatch.setattr(service, "discover_eligible_candidates", lambda: CANDIDATES)
    monkeypatch.setattr(service, "_availability_payload", lambda _providers: {})
    monkeypatch.setattr(service, "_run_router_once", lambda *_args: output or _router_output())


def test_model_recommendation_handler_is_worker_routed():
    from hermes_fork.gateway import _FORK_LONG_HANDLERS

    assert "model_recommendation.get" in _FORK_LONG_HANDLERS


def test_model_recommendation_gateway_returns_explicit_unavailable_without_router_config():
    envelope = srv._methods["model_recommendation.get"](
        1,
        {"draft": "Summarize this short note", "attachments": [], "policy": "balanced"},
    )

    assert envelope["result"] == {
        "status": "unavailable",
        "reason": "Model recommendation router is not configured.",
        "recommendations": [],
    }


def test_model_recommendation_defaults_are_inert_and_not_a_picker_slot():
    slot = DEFAULT_CONFIG["auxiliary"]["model_recommendation"]

    assert slot["provider"] == "auto"
    assert slot["model"] == ""
    assert "model_recommendation" not in __import__("hermes_cli.web_server_config", fromlist=["_AUX_TASK_SLOTS"])._AUX_TASK_SLOTS


def test_gateway_transport_uses_only_configured_authenticated_candidates_and_does_not_mutate_session(monkeypatch):
    _configure_router(monkeypatch)
    session = {"model_override": {"provider": "before"}, "agent": object()}
    srv._sessions["unchanged"] = session
    try:
        envelope = srv.handle_request({
            "id": 2,
            "method": "model_recommendation.get",
            "params": {"draft": "Implement a small change", "attachments": [], "policy": "balanced"},
        })
    finally:
        srv._sessions.pop("unchanged", None)

    assert envelope["result"]["status"] == "ok"
    assert [item["provider"] for item in envelope["result"]["recommendations"]] == ["anthropic", "openai-codex"]
    assert session["model_override"] == {"provider": "before"}


def test_gateway_transport_binds_the_requested_profile_home(monkeypatch, tmp_path):
    profile_home = tmp_path / "profiles" / "draft-review"
    profile_home.mkdir(parents=True)
    seen = {}

    monkeypatch.setattr(srv, "_profile_home", lambda profile: profile_home if profile == "draft-review" else None)

    def fake_recommend(**_kwargs):
        from hermes_constants import get_hermes_home

        seen["home"] = get_hermes_home()
        return service.unavailable()

    monkeypatch.setattr(service, "recommend", fake_recommend)
    envelope = srv.handle_request({
        "id": 3,
        "method": "model_recommendation.get",
        "params": {"profile": "draft-review", "draft": "Draft", "attachments": []},
    })

    assert envelope["result"]["status"] == "unavailable"
    assert seen["home"] == profile_home


def test_invalid_router_configuration_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"auxiliary": {"model_recommendation": {"provider": "anthropic", "model": "router", "timeout": "invalid"}}},
    )

    assert service.recommend(draft="Assess", attachments=[], policy="balanced") == service.unavailable()


def test_candidate_discovery_filters_to_authenticated_configured_routes(monkeypatch):
    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: object())
    monkeypatch.setattr(
        "hermes_cli.inventory.build_model_options_payload",
        lambda *_args, **_kwargs: {
            "providers": [
                {"slug": "anthropic", "authenticated": True, "models": ["configured"],
                 "capabilities": {"configured": {"reasoning": True, "fast": True}},
                 "pricing": {"configured": {"free": True}}},
                {"slug": "openai-codex", "authenticated": False, "models": ["not-eligible"]},
            ],
        },
    )

    assert service.discover_eligible_candidates() == [{
        "provider": "anthropic", "model": "configured",
        "capabilities": {"reasoning": True, "fast": True, "effort_options": list(service.EFFORTS)},
        "cost": "free",
    }]


def test_policy_presets_rank_the_same_eligible_routes_differently():
    parsed = service._parse_router_output(_router_output(), CANDIDATES)
    assert parsed is not None

    balanced = service.rank_recommendations(parsed, {}, "balanced")
    save_codex = service.rank_recommendations(parsed, {}, "save_codex")
    best_quality = service.rank_recommendations(parsed, {}, "best_quality")

    assert balanced[0]["provider"] == "anthropic"
    assert save_codex[0]["provider"] == "anthropic"
    assert best_quality[0]["provider"] == "openai-codex"


def test_stale_and_unavailable_account_data_are_explicit(monkeypatch):
    stale = AccountUsageSnapshot(provider="anthropic", source="usage", fetched_at=datetime.now(timezone.utc) - timedelta(hours=1))
    unavailable = AccountUsageSnapshot(
        provider="openai-codex", source="usage", fetched_at=datetime.now(timezone.utc), unavailable_reason="sign in required"
    )
    monkeypatch.setattr("hermes_fork.account_limits.service.fetch_account_limits", lambda _providers: (stale, unavailable))

    availability = service._availability_payload({"anthropic", "openai-codex"})

    assert availability["anthropic"]["status"] == "stale"
    assert availability["openai-codex"]["status"] == "unavailable"


def test_router_is_a_single_direct_call_with_strict_structured_output(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *args, **kwargs: (client, "router"))

    raw = service._run_router_once({"provider": "anthropic", "model": "router", "base_url": None, "api_key": None, "api_mode": None, "timeout": 20}, [])

    assert raw == "{}"
    assert len(calls) == 1
    assert calls[0]["response_format"]["json_schema"]["strict"] is True


def test_malformed_router_output_fails_closed(monkeypatch):
    _configure_router(monkeypatch, output='{"recommendations": []}')

    result = service.recommend(draft="Plan a migration", attachments=[], policy="balanced")

    assert result == {
        "status": "unavailable",
        "reason": "Model recommendation router returned invalid structured output.",
        "recommendations": [],
    }


def test_router_input_contains_draft_and_safe_attachment_metadata_only(monkeypatch):
    captured = []
    _configure_router(monkeypatch)
    monkeypatch.setattr(service, "_run_router_once", lambda _router, messages: captured.extend(messages) or _router_output(ambiguous=True))

    result = service.recommend(
        draft="Assess this draft",
        attachments=[{"name": "brief.pdf", "mime_type": "application/pdf", "size": 42, "bytes": "SECRET-BYTES", "path": "/private"}],
        policy="best_quality",
    )

    wire = captured[1]["content"]
    assert "Assess this draft" in wire
    assert "brief.pdf" in wire
    assert "SECRET-BYTES" not in wire
    assert "/private" not in wire
    assert "transcript" not in wire
    assert "Draft-only assessment is ambiguous" in result["recommendations"][0]["reason"]
    assert result["recommendations"][0]["effort"] == "high"


def test_router_output_omitting_an_eligible_provider_fails_closed():
    incomplete = (
        '{"task_risk":"low","ambiguous":false,"recommendations":['
        '{"provider":"anthropic","model":"claude-fast","effort":"low","reason":"Adequate","quality":60,"materially_advantageous":false}]}'
    )

    assert service._parse_router_output(incomplete, CANDIDATES) is None
