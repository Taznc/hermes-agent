"""Kanban model-routing policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.config import load_config_readonly


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _routing_config(*, enabled: bool = True):
    return {
        "kanban": {
            "model_routing": {
                "enabled": enabled,
                "classifier": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4-mini",
                    "max_input_tokens": 8000,
                },
                "routes": {
                    "mechanical": {
                        "provider": "openai-codex",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "medium",
                    }
                },
            }
        }
    }


def test_model_routing_defaults_disabled_and_returns_default(kanban_home):
    cfg = load_config_readonly()
    assert cfg["kanban"]["model_routing"]["enabled"] is False

    from hermes_cli.kanban_model_routing import resolve_kanban_model_route

    decision = resolve_kanban_model_route(title="Update README", body="Tweak docs text")

    assert decision.route_source == "default"
    assert decision.route_name is None
    assert decision.model_override is None
    assert decision.provider_override is None
    assert decision.reasoning_effort is None


def test_explicit_override_short_circuits_classifier_call(kanban_home, monkeypatch):
    from hermes_cli import kanban_model_routing as kmr

    called = []

    def _boom(*_args, **_kwargs):
        called.append(("called", _args, _kwargs))
        raise AssertionError("classifier must not run when the card already has an explicit override")

    monkeypatch.setattr(kmr, "_call_llm", _boom)

    decision = kmr.resolve_kanban_model_route(
        title="Fix it",
        body="Keep the explicit pin",
        explicit_model="claude-sonnet-4",
        explicit_provider="anthropic",
        explicit_reasoning_effort="high",
        config=_routing_config(enabled=True),
    )

    assert called == []
    assert decision.route_source == "explicit"
    assert decision.route_name is None
    assert decision.model_override == "claude-sonnet-4"
    assert decision.provider_override == "anthropic"
    assert decision.reasoning_effort == "high"


@pytest.mark.parametrize("content", ["not json", json.dumps({"route": "bogus"})])
def test_malformed_classifier_output_fails_closed_to_default(kanban_home, monkeypatch, content):
    from hermes_cli import kanban_model_routing as kmr

    calls = []

    class _Resp:
        def __init__(self, text: str) -> None:
            self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": text})()})()]

    def _fake_llm(*_args, **_kwargs):
        calls.append(("called", _args, _kwargs))
        return _Resp(content)

    monkeypatch.setattr(kmr, "_call_llm", _fake_llm)

    decision = kmr.resolve_kanban_model_route(
        title="Update docs",
        body="Small mechanical edit",
        config=_routing_config(enabled=True),
    )

    assert len(calls) == 1
    assert decision.route_source == "default"
    assert decision.route_name is None
    assert decision.model_override is None
    assert decision.provider_override is None
    assert decision.reasoning_effort is None


def test_classifier_failure_fails_closed_to_default(kanban_home, monkeypatch):
    from hermes_cli import kanban_model_routing as kmr

    calls = []

    def _fake_llm(*_args, **_kwargs):
        calls.append(("called", _args, _kwargs))
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(kmr, "_call_llm", _fake_llm)

    decision = kmr.resolve_kanban_model_route(
        title="Update docs",
        body="Small mechanical edit",
        config=_routing_config(enabled=True),
    )

    assert len(calls) == 1
    assert decision.route_source == "default"
    assert decision.route_name is None
    assert decision.model_override is None
    assert decision.provider_override is None
    assert decision.reasoning_effort is None


def test_retryable_classifier_failure_uses_exactly_one_model_call_and_fails_closed(
    kanban_home, monkeypatch,
):
    from agent import auxiliary_client as aux
    from hermes_cli import kanban_model_routing as kmr

    calls = []

    def _fake_relay(*_args, **_kwargs):
        calls.append(("called", _args, _kwargs))
        raise ConnectionResetError("temporary transport failure")

    monkeypatch.setattr(aux, "_relay_sync_completion", _fake_relay)
    monkeypatch.setattr(aux, "_resolve_task_provider_model", lambda *args, **kwargs: (
        "openai-codex", "gpt-5.4-mini", None, None, None,
    ))
    monkeypatch.setattr(aux, "_get_cached_client", lambda *args, **kwargs: (
        type("FakeClient", (), {"chat": type("Chat", (), {"completions": type("Completions", (), {})()})()})(),
        "gpt-5.4-mini",
    ))

    decision = kmr.resolve_kanban_model_route(
        title="Update docs",
        body="Small mechanical edit",
        config=_routing_config(enabled=True),
    )

    assert len(calls) == 1
    assert decision.route_source == "default"
    assert decision.route_name is None
    assert decision.model_override is None
    assert decision.provider_override is None
    assert decision.reasoning_effort is None


def test_safe_classifier_selects_mechanical_route_with_one_model_call(kanban_home, monkeypatch):
    from hermes_cli import kanban_model_routing as kmr

    calls = []

    class _Resp:
        def __init__(self, text: str) -> None:
            self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": text})()})()]

    def _fake_llm(*_args, **_kwargs):
        calls.append(("called", _args, _kwargs))
        return _Resp(json.dumps({"route": "mechanical"}))

    monkeypatch.setattr(kmr, "_call_llm", _fake_llm)

    decision = kmr.resolve_kanban_model_route(
        title="Tighten the README wording",
        body="The change is a narrow docs tweak.",
        config=_routing_config(enabled=True),
    )

    assert len(calls) == 1
    assert decision.route_source == "mechanical"
    assert decision.route_name == "mechanical"
    assert decision.model_override == "gpt-5.4-mini"
    assert decision.provider_override == "openai-codex"
    assert decision.reasoning_effort == "medium"
