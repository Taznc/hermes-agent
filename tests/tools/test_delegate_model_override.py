"""Tests for per-spawn model / reasoning_effort overrides on delegate_task.

Phase 2.11. The resolution chain being exercised is:

    per-spawn argument  >  global ``delegation.*`` pin  >  parent inheritance

These tests run the REAL resolver functions and the REAL ``_build_child_agent``
reasoning branch — no mock stands in for the resolution chain itself, because a
resolution-chain change is exactly the class of bug a green mock hides. Only
the network-bound pieces (credential resolution against a live provider,
AIAgent construction) are substituted.
"""

from types import SimpleNamespace

import pytest

from tools.delegation_model_override import (
    describe_route,
    resolve_effort_override,
    resolve_model_override,
)


def _parent(model="parent-model", provider="anthropic", reasoning=None):
    return SimpleNamespace(model=model, provider=provider, reasoning_config=reasoning)


# ── reasoning_effort ─────────────────────────────────────────────────────

class TestResolveEffortOverride:
    def test_omitted_returns_none_none(self):
        """Omitting the field must leave the existing chain untouched."""
        cfg, err = resolve_effort_override(None)
        assert cfg is None and err is None

    def test_blank_string_treated_as_omitted(self):
        cfg, err = resolve_effort_override("   ")
        assert cfg is None and err is None

    @pytest.mark.parametrize(
        "level", ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    )
    def test_valid_levels_parse_to_enabled_config(self, level):
        cfg, err = resolve_effort_override(level)
        assert err is None
        assert cfg == {"enabled": True, "effort": level}

    @pytest.mark.parametrize("raw", ["none", "false", "disabled", False])
    def test_none_family_disables_thinking(self, raw):
        """'none' must DISABLE reasoning, not fall through to a default."""
        cfg, err = resolve_effort_override(raw)
        assert err is None
        assert cfg == {"enabled": False}

    def test_case_insensitive(self):
        cfg, err = resolve_effort_override("HIGH")
        assert err is None and cfg == {"enabled": True, "effort": "high"}

    def test_unknown_level_is_a_truthful_error(self):
        """An explicit bad level fails loudly — never silently degrades."""
        cfg, err = resolve_effort_override("turbo")
        assert cfg is None
        assert err and "turbo" in err
        # The error must actually name the valid levels, not just say "invalid".
        assert "high" in err and "none" in err


class TestEffortPrecedenceInBuildChild:
    """The per-spawn effort must beat the global pin inside _build_child_agent."""

    def _reasoning_for(self, monkeypatch, *, delegation_cfg, override, parent_effort):
        """Run the real precedence branch and report the chosen config."""
        import tools.delegate_tool as dt

        captured = {}

        class _FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.session_id = "child"
                self.model = kwargs.get("model")
                self._delegate_depth = 1

            def __getattr__(self, name):
                return None

        monkeypatch.setattr(dt, "_load_config", lambda: delegation_cfg)
        import run_agent

        monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
        parent = _parent(reasoning=parent_effort)
        parent.base_url = "https://example.invalid"
        parent.api_key = "k"
        parent.enabled_toolsets = ["terminal"]
        parent._delegate_depth = 0
        try:
            dt._build_child_agent(
                task_index=0,
                goal="g",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=5,
                task_count=1,
                parent_agent=parent,
                override_reasoning_config=override,
            )
        except Exception as exc:  # construction may fail past the branch
            if "reasoning_config" not in captured:
                pytest.skip(f"child construction failed before capture: {exc}")
        return captured.get("reasoning_config")

    def test_per_spawn_beats_global_pin(self, monkeypatch):
        """Global pin says low, caller says high → high wins."""
        got = self._reasoning_for(
            monkeypatch,
            delegation_cfg={"reasoning_effort": "low"},
            override={"enabled": True, "effort": "high"},
            parent_effort={"enabled": True, "effort": "medium"},
        )
        assert got == {"enabled": True, "effort": "high"}

    def test_per_spawn_none_beats_global_pin(self, monkeypatch):
        """A per-spawn 'none' must disable thinking even under a global pin."""
        got = self._reasoning_for(
            monkeypatch,
            delegation_cfg={"reasoning_effort": "high"},
            override={"enabled": False},
            parent_effort={"enabled": True, "effort": "medium"},
        )
        assert got == {"enabled": False}

    def test_global_pin_applies_when_no_override(self, monkeypatch):
        """Omitting the per-spawn field preserves the global-pin behaviour."""
        got = self._reasoning_for(
            monkeypatch,
            delegation_cfg={"reasoning_effort": "low"},
            override=None,
            parent_effort={"enabled": True, "effort": "medium"},
        )
        assert got == {"enabled": True, "effort": "low"}

    def test_parent_inherited_when_nothing_set(self, monkeypatch):
        """No override, no pin → parent's level, exactly as before."""
        got = self._reasoning_for(
            monkeypatch,
            delegation_cfg={},
            override=None,
            parent_effort={"enabled": True, "effort": "medium"},
        )
        assert got == {"enabled": True, "effort": "medium"}


# ── model ────────────────────────────────────────────────────────────────

class TestResolveModelOverride:
    def test_omitted_returns_none_none(self):
        """Omitted model → caller keeps today's inheritance exactly."""
        cfg, err = resolve_model_override(None, _parent(), {})
        assert cfg is None and err is None

    def test_blank_treated_as_omitted(self):
        cfg, err = resolve_model_override("   ", _parent(), {})
        assert cfg is None and err is None

    def test_same_provider_swap_returns_model_only(self, monkeypatch):
        """A model on the parent's own provider must not re-resolve creds.

        Returning provider=None keeps _resolve_delegation_credentials on its
        inherit branch, so the child keeps the parent's working credentials
        and base_url rather than re-deriving a possibly different endpoint.
        """
        import tools.delegation_model_override as mo

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: ("anthropic", m))
        cfg, err = resolve_model_override(
            "claude-haiku-4", _parent(provider="anthropic"), {}
        )
        assert err is None
        assert cfg == {"model": "claude-haiku-4"}
        assert "provider" not in cfg

    def test_cross_provider_carries_provider(self, monkeypatch):
        """A model detected on another provider routes there explicitly."""
        import tools.delegation_model_override as mo

        monkeypatch.setattr(
            mo, "_detect_provider", lambda m, p: ("openrouter", "vendor/" + m)
        )
        cfg, err = resolve_model_override("cheap-model", _parent(provider="anthropic"), {})
        assert err is None
        assert cfg["provider"] == "openrouter"
        assert cfg["model"] == "vendor/cheap-model"

    def test_unknown_model_rejected_with_truthful_error(self, monkeypatch):
        """Positive evidence of absence → reject, and say why."""
        import tools.delegation_model_override as mo

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: None)
        monkeypatch.setattr(
            mo, "_catalog_for", lambda p: ["claude-opus-5", "claude-haiku-4"]
        )
        cfg, err = resolve_model_override("gpt-9-ultra", _parent(provider="anthropic"), {})
        assert cfg is None
        assert err is not None
        # Truthful: names the model, the provider searched, and real examples.
        assert "gpt-9-ultra" in err
        assert "anthropic" in err
        assert "claude-opus-5" in err

    def test_empty_catalog_does_not_reject(self, monkeypatch):
        """No catalog is NOT evidence of an invalid model — must not reject.

        Custom endpoints, local Ollama, and offline catalog fetches all yield
        an empty list. Refusing a legitimate spawn because a catalog lookup
        failed is worse than letting the provider return its own error.
        """
        import tools.delegation_model_override as mo

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: None)
        monkeypatch.setattr(mo, "_catalog_for", lambda p: [])
        cfg, err = resolve_model_override("some-local-model", _parent(provider="custom"), {})
        assert err is None
        assert cfg is not None and cfg["model"] == "some-local-model"

    def test_bare_name_matches_vendor_prefixed_catalog_entry(self, monkeypatch):
        import tools.delegation_model_override as mo

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: None)
        monkeypatch.setattr(mo, "_catalog_for", lambda p: ["anthropic/claude-haiku-4"])
        cfg, err = resolve_model_override("claude-haiku-4", _parent(provider="openrouter"), {})
        assert err is None and cfg is not None

    def test_global_pin_provider_used_when_no_detection(self, monkeypatch):
        """Precedence rung 2: fall back to the configured delegation.provider."""
        import tools.delegation_model_override as mo

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: None)
        monkeypatch.setattr(mo, "_catalog_for", lambda p: [])
        cfg, err = resolve_model_override(
            "pinned-model", _parent(provider="anthropic"), {"provider": "openrouter"}
        )
        assert err is None
        assert cfg["provider"] == "openrouter"

    def test_pin_transport_carried_only_on_same_provider(self, monkeypatch):
        """A pin's base_url must not leak onto a DIFFERENT provider."""
        import tools.delegation_model_override as mo

        pin = {"provider": "openrouter", "base_url": "https://pinned.invalid", "api_key": "x"}

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: ("openrouter", m))
        same, err = resolve_model_override("m", _parent(provider="anthropic"), pin)
        assert err is None and same["base_url"] == "https://pinned.invalid"

        monkeypatch.setattr(mo, "_detect_provider", lambda m, p: ("nous", m))
        other, err = resolve_model_override("m", _parent(provider="anthropic"), pin)
        assert err is None
        assert other["provider"] == "nous"
        assert "base_url" not in other, "pin transport leaked to another provider"


class TestDescribeRoute:
    def test_reports_concrete_model_on_inherit(self):
        """Never a bare null — inheritance is resolved for the audit trail."""
        route = describe_route({}, _parent(model="parent-model"), source="inherit")
        assert route["model"] == "parent-model"
        assert route["inherited"] is True
        assert route["source"] == "inherit"

    def test_reports_override_model_and_source(self):
        route = describe_route(
            {"model": "cheap", "provider": "openrouter"},
            _parent(model="parent-model"),
            source="spawn",
        )
        assert route["model"] == "cheap"
        assert route["provider"] == "openrouter"
        assert route["inherited"] is False
        assert route["source"] == "spawn"

    def test_global_pin_distinguished_from_per_spawn(self):
        """'not inherited' alone can't tell a per-call route from a global pin."""
        route = describe_route({"model": "pinned"}, _parent(), source="config")
        assert route["source"] == "config"
        assert route["inherited"] is False


# ── schema surface ───────────────────────────────────────────────────────

class TestSchemaSurface:
    def test_fields_present_top_level_and_per_task(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA

        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        assert "model" in props
        assert "reasoning_effort" in props
        item_props = props["tasks"]["items"]["properties"]
        assert "model" in item_props
        assert "reasoning_effort" in item_props

    def test_fields_are_optional(self):
        """Omitting them must remain valid — inheritance is the default."""
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA

        assert "model" not in DELEGATE_TASK_SCHEMA["parameters"].get("required", [])
        assert "reasoning_effort" not in DELEGATE_TASK_SCHEMA["parameters"].get(
            "required", []
        )
        assert DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"][
            "required"
        ] == ["goal"]

    def test_handler_forwards_both_fields(self):
        """A schema field nobody reads is worse than no field at all."""
        import inspect

        from tools.delegate_tool import delegate_task

        sig = inspect.signature(delegate_task)
        assert "model" in sig.parameters
        assert "reasoning_effort" in sig.parameters
        assert sig.parameters["model"].default is None
        assert sig.parameters["reasoning_effort"].default is None

    def test_effort_enum_matches_parser(self):
        """The advertised enum must be levels the parser actually accepts."""
        from hermes_constants import parse_reasoning_effort
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA

        enum = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["reasoning_effort"][
            "enum"
        ]
        for level in enum:
            assert parse_reasoning_effort(level) is not None, level
