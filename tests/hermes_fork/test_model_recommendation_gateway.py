from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

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


def _sandbox_profiles(monkeypatch, tmp_path, *names):
    """Real profile directories under a sandboxed HOME (profile roots are HOME-anchored)."""
    root = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / ".config" / "gh"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(srv, "_hermes_home", root)
    root.mkdir(parents=True, exist_ok=True)
    homes = {name: root / "profiles" / name for name in names}
    for home in homes.values():
        home.mkdir(parents=True)
    return root, homes


def test_gateway_transport_binds_the_requested_profile_home(monkeypatch, tmp_path):
    _root, homes = _sandbox_profiles(monkeypatch, tmp_path, "draft-review")
    profile_home = homes["draft-review"]
    seen = {}

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


@pytest.mark.parametrize("profile", [
    pytest.param("missing", id="unknown-profile"),
    pytest.param("Missing-Profile", id="mixed-case-unknown-profile"),
    pytest.param("../outside", id="invalid-name"),
    pytest.param("removed", id="tombstoned-profile"),
    pytest.param(["draft-review"], id="non-string"),
])
def test_explicit_invalid_profile_is_rejected_before_any_router_call(monkeypatch, tmp_path, profile):
    from hermes_constants import mark_named_profile_deleted

    _root, homes = _sandbox_profiles(monkeypatch, tmp_path, "draft-review", "removed")
    mark_named_profile_deleted(homes["removed"])
    calls = []

    def fake_recommend(**kwargs):
        from hermes_constants import get_hermes_home

        calls.append(get_hermes_home())
        return service.unavailable()

    monkeypatch.setattr(service, "recommend", fake_recommend)
    envelope = srv.handle_request({
        "id": 4,
        "method": "model_recommendation.get",
        "params": {"profile": profile, "draft": "PRIVATE-DRAFT", "attachments": [], "policy": "best_quality"},
    })

    # Fail closed: an explicitly named profile that does not resolve must never fall back to
    # the launch profile's router configuration — that would send the unsent draft elsewhere.
    assert envelope.get("error", {}).get("code") == 4002, envelope
    assert "PRIVATE-DRAFT" not in json.dumps(envelope)
    assert calls == []


def test_omitted_and_launch_profile_requests_keep_running_against_launch_home(monkeypatch, tmp_path):
    root, _homes = _sandbox_profiles(monkeypatch, tmp_path, "draft-review")
    seen = []

    def fake_recommend(**_kwargs):
        from hermes_constants import get_hermes_home

        seen.append(get_hermes_home())
        return service.unavailable()

    monkeypatch.setattr(service, "recommend", fake_recommend)
    for params in ({"draft": "Draft"}, {"draft": "Draft", "profile": None}, {"draft": "Draft", "profile": ""},
                   {"draft": "Draft", "profile": " "}, {"draft": "Draft", "profile": "default"},
                   {"draft": "Draft", "profile": "DEFAULT"}):
        envelope = srv.handle_request({"id": 5, "method": "model_recommendation.get", "params": params})
        assert envelope["result"]["status"] == "unavailable", (params, envelope)
    assert seen == [root] * 6


def test_gateway_dispatch_uses_temporary_profile_config_and_real_candidate_discovery(monkeypatch, tmp_path):
    profile_home = tmp_path / ".hermes" / "profiles" / "draft-review"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    models: [profile-only-test-model]\n"
        "auxiliary:\n"
        "  model_recommendation:\n"
        "    provider: anthropic\n"
        "    model: claude-3-5-haiku-latest\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / ".config" / "gh"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-key")
    captured = []

    def router_response(_router, messages):
        captured.extend(messages)
        request = json.loads(messages[1]["content"])
        selected = {}
        for candidate in request["candidates"]:
            selected.setdefault(candidate["provider"], candidate)
        return json.dumps({
            "task_risk": "low", "ambiguous": False,
            "recommendations": [{
                "provider": candidate["provider"], "model": candidate["model"], "effort": "none",
                "reason": "Adequate", "quality": 50, "materially_advantageous": False,
            } for candidate in selected.values()],
        })

    monkeypatch.setattr(service, "_run_router_once", router_response)

    class RecordingTransport:
        def __init__(self):
            self.responses = []
            self.done = threading.Event()

        def write(self, response):
            self.responses.append(response)
            self.done.set()
            return True

        def close(self):
            return None

    transport = RecordingTransport()
    assert srv.dispatch({
        "id": "profile-transport", "method": "model_recommendation.get",
        "params": {"profile": "draft-review", "draft": "Assess this draft", "attachments": []},
    }, transport=transport) is None
    assert transport.done.wait(timeout=5)

    assert transport.responses[0]["result"]["status"] == "ok", transport.responses
    candidate_routes = {(candidate["provider"], candidate["model"])
                        for candidate in json.loads(captured[1]["content"])["candidates"]}
    assert {provider for provider, _model in candidate_routes} == {"anthropic"}
    assert ("anthropic", "profile-only-test-model") in candidate_routes
    providers = [item["provider"] for item in transport.responses[0]["result"]["recommendations"]]
    assert "anthropic" in providers
    assert "Assess this draft" in captured[1]["content"]


def test_invalid_router_configuration_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"auxiliary": {"model_recommendation": {"provider": "anthropic", "model": "router", "timeout": "invalid"}}},
    )

    assert service.recommend(draft="Assess", attachments=[], policy="balanced") == service.unavailable()


def test_candidate_discovery_filters_to_authenticated_configured_routes(monkeypatch):
    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: SimpleNamespace(user_providers={}))
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


def test_candidate_discovery_excludes_models_marked_unavailable_by_inventory(monkeypatch):
    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: SimpleNamespace(user_providers={}))
    monkeypatch.setattr(
        "hermes_cli.inventory.build_model_options_payload",
        lambda *_args, **_kwargs: {
            "providers": [{
                "slug": "anthropic", "authenticated": True,
                "models": ["available", "locked"], "unavailable_models": ["locked"],
                "capabilities": {"available": {"reasoning": True}, "locked": {"reasoning": True}},
            }],
        },
    )

    assert [candidate["model"] for candidate in service.discover_eligible_candidates()] == ["available"]


def test_policy_presets_rank_the_same_eligible_routes_by_their_distinct_semantics():
    candidates = [
        {**CANDIDATES[0], "cost": "free"},
        {**CANDIDATES[1], "cost": "free"},
        {
            "provider": "openai", "model": "quality-first",
            "capabilities": {"reasoning": True, "fast": False, "effort_options": list(service.EFFORTS)},
            "cost": "paid_or_unknown",
        },
    ]
    raw = json.dumps({
        "task_risk": "medium", "ambiguous": False,
        "recommendations": [
            {"provider": "anthropic", "model": "claude-fast", "effort": "medium", "reason": "Adequate", "quality": 80,
             "materially_advantageous": False},
            {"provider": "openai-codex", "model": "codex-strong", "effort": "high", "reason": "Strong", "quality": 90,
             "materially_advantageous": False},
            {"provider": "openai", "model": "quality-first", "effort": "high", "reason": "Strongest", "quality": 100,
             "materially_advantageous": False},
        ],
    })
    parsed = service._parse_router_output(raw, candidates)
    assert parsed is not None

    balanced = service.rank_recommendations(parsed, {}, "balanced")
    save_codex = service.rank_recommendations(parsed, {}, "save_codex")
    best_quality = service.rank_recommendations(parsed, {}, "best_quality")

    assert balanced[0]["provider"] == "openai-codex"  # cheapest adequate route
    assert save_codex[0]["provider"] == "anthropic"  # preserves non-advantageous Codex capacity
    assert best_quality[0]["provider"] == "openai"  # strongest eligible route


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


def _schema_numeric_bound_paths(node, path=""):
    """Walk a JSON schema and yield paths of any 'minimum'/'maximum' keyword."""
    if isinstance(node, dict):
        for key in ("minimum", "maximum"):
            if key in node:
                yield f"{path}.{key}"
        for key, value in node.items():
            yield from _schema_numeric_bound_paths(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _schema_numeric_bound_paths(item, f"{path}[{i}]")


def test_output_schema_has_no_anthropic_incompatible_numeric_bounds():
    """Anthropic's structured-output validator 400s with 'properties maximum, minimum are not
    supported' for integer/number schema properties (confirmed live against claude-sonnet-5,
    2026-09-07). The router's shared _OUTPUT_SCHEMA must never reintroduce minimum/maximum;
    range checks belong in _parse_router_output instead."""
    bounds = list(_schema_numeric_bound_paths(service._OUTPUT_SCHEMA["schema"]))
    assert bounds == [], f"schema has Anthropic-incompatible numeric bounds: {bounds}"


def test_run_router_once_disables_thinking_only_for_anthropic_wire(monkeypatch):
    """Adaptive-thinking Claude models (Sonnet/Opus/Fable 4.6+) think by default even with no
    reasoning config, and invisible thinking counts against the fixed 900-token budget — this
    truncated real router output (finish_reason="length") and always failed closed. Disabling
    thinking must be scoped to Anthropic-Messages-wire clients only; a plain OpenAI-compatible
    client has no ``_reasoning_config`` kwarg and would reject it."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *args, **kwargs: (client, "router"))

    service._run_router_once(
        {"provider": "anthropic", "model": "router", "base_url": None, "api_key": None, "api_mode": None, "timeout": 20}, [])
    assert calls[-1].get("_reasoning_config") == {"enabled": False}

    service._run_router_once(
        {"provider": "openai-codex", "model": "router", "base_url": None, "api_key": None, "api_mode": None, "timeout": 20}, [])
    assert "_reasoning_config" not in calls[-1]


def test_codex_adapter_translates_router_schema_to_responses_output_format():
    from agent.auxiliary_client import _CodexCompletionsAdapter

    adapter = _CodexCompletionsAdapter(SimpleNamespace(base_url="https://api.openai.com/v1"), "gpt-5.3-codex")
    responses_kwargs, _model, _timeout = adapter._build_responses_kwargs({
        "model": "gpt-5.3-codex",
        "messages": [{"role": "system", "content": "Return JSON"}, {"role": "user", "content": "Draft"}],
        "response_format": {"type": "json_schema", "json_schema": service._OUTPUT_SCHEMA},
    })

    assert responses_kwargs["text"]["format"] == {
        "type": "json_schema",
        "name": "model_recommendations",
        "schema": service._OUTPUT_SCHEMA["schema"],
        "strict": True,
    }


def test_malformed_router_output_fails_closed(monkeypatch):
    _configure_router(monkeypatch, output='{"recommendations": []}')

    result = service.recommend(draft="Plan a migration", attachments=[], policy="balanced")

    assert result == {
        "status": "unavailable",
        "reason": "Model recommendation router returned invalid structured output.",
        "recommendations": [],
    }


def test_router_output_with_boolean_quality_fails_closed():
    invalid = _router_output().replace('"quality":60', '"quality":true')

    assert service._parse_router_output(invalid, CANDIDATES) is None


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


def test_v1_input_bounds_and_partial_metadata_remain_compatible():
    # Python counts Unicode code points, not the renderer's UTF-16 code units.
    assert "result" in srv.handle_request({"id": 10, "method": "model_recommendation.get",
                                         "params": {"draft": "😀" * 100_000}})
    for draft in ("", " \n\t", None, "a" * 100_001):
        result = srv.handle_request({"id": 11, "method": "model_recommendation.get", "params": {"draft": draft}})
        assert result["error"]["code"] == 4000
    metadata = [{"name": "a" * 256, "kind": "file", "size": 2**53},
                {"name": "b" * 257, "mime_type": None, "size": 2**53 + 1},
                {"kind": "image", "path": "/not-forwarded"}, {}]
    assert service._safe_attachment_metadata(metadata) == [
        {"name": "a" * 256, "kind": "file", "size": 2**53}, {}, {"kind": "image"}, {}]
    # Preserve the parent's first-32 cap. The consumer must refuse >32 rather
    # than silently request advice on a partial attachment inventory.
    assert service._safe_attachment_metadata([{"name": str(i)} for i in range(33)]) == [
        {"name": str(i)} for i in range(32)]
