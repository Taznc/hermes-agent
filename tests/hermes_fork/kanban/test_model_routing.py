"""Fork-owned tests for ``hermes_fork.kanban.model_routing``.

Extraction target: unattended model/provider/reasoning-effort policy
validation and per-profile route resolution, moved out of
``hermes_cli.kanban_db`` behind the
``# >>> FORK ANCHOR: kanban-model-routing <<<`` marker. See
``tests/hermes_cli/test_kanban_model_policy.py`` for end-to-end
dispatcher-integration coverage of this same logic reached through the
``kanban_db`` facade; these tests instead pin the extracted module's own
contracts: the pure policy gate in isolation, and that the re-exported
facade attributes are identity-equal to the extracted functions (proving
the anchor wires the fork module in rather than duplicating behavior that
could drift).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_fork.kanban import model_routing as mr


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PIN_HOME", raising=False)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Re-export identity: the facade attribute IS the extracted function, not a
# copy — proves the anchor import wires the fork module in rather than
# duplicating behavior that could drift.
# ---------------------------------------------------------------------------


def test_kanban_db_reexports_the_extracted_policy_gate():
    assert kb.validate_model_effort_policy is mr.validate_model_effort_policy
    assert kb.validate_task_model_policy is mr.validate_task_model_policy
    assert kb.validate_review_task_model_policy is mr.validate_review_task_model_policy
    assert kb.set_reasoning_effort is mr.set_reasoning_effort
    assert kb.set_route_overrides is mr.set_route_overrides
    assert kb.ModelPolicyDecision is mr.ModelPolicyDecision


def test_extraction_late_bound_origin_resolves_to_the_real_module():
    """The cycle-breaking ``_kb`` module ref must point at the real,
    fully-initialized origin module, not a stand-in or partial import."""
    assert mr._kb is kb


# ---------------------------------------------------------------------------
# Pure policy gate — behavior contracts, not change detectors
# ---------------------------------------------------------------------------


def test_unknown_route_is_refused_even_unforced():
    with pytest.raises(ValueError, match="refuses"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="low",
            assignee="worker",
        )


def test_approved_route_is_accepted_unforced():
    decision = mr.validate_model_effort_policy(
        provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="medium",
        assignee="worker",
    )
    assert decision.forced is False


def test_free_tier_and_mini_models_are_categorically_denied_even_forced():
    for model in ("gpt-5.6-mini", "some-model:free", "some-model/free"):
        with pytest.raises(ValueError, match="denies"):
            mr.validate_model_effort_policy(
                provider="openai-codex", model=model, reasoning_effort="low",
                assignee="worker", force=True, force_reason="approved", forced_by="operator",
            )


def test_force_requires_both_reason_and_actor():
    with pytest.raises(ValueError, match="policy_force=True"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="medium",
            assignee="worker", force_reason="approved",
        )
    with pytest.raises(ValueError, match="non-empty reason"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="gpt-6-astra", reasoning_effort="high",
            assignee="worker", force=True, forced_by="operator",
        )
    with pytest.raises(ValueError, match="operator profile"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="gpt-6-astra", reasoning_effort="high",
            assignee="worker", force=True, force_reason="approved",
        )


def test_forced_astra_route_is_accepted_and_records_reason_and_route():
    decision = mr.validate_model_effort_policy(
        provider="openai-codex", model="gpt-6-astra", reasoning_effort="high",
        assignee="worker", force=True, force_reason="approved for a hard task",
        forced_by="operator",
    )
    assert decision.forced is True
    assert decision.force_reason == "approved for a hard task"
    assert decision.force_route is not None


def test_luna_is_ineligible_for_reviewer_and_debugger_profiles():
    for assignee in ("reviewer", "debugger"):
        with pytest.raises(ValueError, match="mechanical-only Luna"):
            mr.validate_model_effort_policy(
                provider="openai-codex", model="gpt-5.6-luna", reasoning_effort="low",
                assignee=assignee,
            )
    # A non-listed profile may still use Luna at its one approved effort.
    decision = mr.validate_model_effort_policy(
        provider="openai-codex", model="gpt-5.6-luna", reasoning_effort="low",
        assignee="builder",
    )
    assert decision.forced is False


def test_moa_routes_are_forbidden_regardless_of_force():
    with pytest.raises(ValueError, match="MoA"):
        mr.validate_model_effort_policy(
            provider="moa", model="gpt-5.6-sol", reasoning_effort="medium", assignee="worker",
        )
    with pytest.raises(ValueError, match="MoA"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="moa:whatever", reasoning_effort="medium",
            assignee="worker",
        )


def test_hidden_fallback_routes_are_refused():
    with pytest.raises(ValueError, match="fallback"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="medium",
            assignee="worker", policy={"_fallback_routes": "anthropic/claude"},
        )


def test_allowed_routes_policy_restricts_but_cannot_expand():
    # Restricting to a subset of the built-in allowlist is honored.
    restricted = {"allowed_routes": [
        {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium"},
    ]}
    decision = mr.validate_model_effort_policy(
        provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="medium",
        assignee="worker", policy=restricted,
    )
    assert decision.forced is False
    with pytest.raises(ValueError, match="refuses"):
        mr.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-terra", reasoning_effort="medium",
            assignee="worker", policy=restricted,
        )
    # Trying to expand the allowlist with an unsupported route is rejected outright.
    with pytest.raises(ValueError, match="unsupported route"):
        mr._configured_policy_routes(
            {"allowed_routes": [
                {"provider": "anthropic", "model": "claude-x", "reasoning_effort": "high"},
            ]}
        )


# ---------------------------------------------------------------------------
# Route resolution / override plumbing
# ---------------------------------------------------------------------------


def test_validate_model_override_requires_model_with_provider():
    assert mr._validate_model_override(None, None) == (None, None)
    assert mr._validate_model_override("gpt-5.6-sol", "openai-codex") == ("gpt-5.6-sol", "openai-codex")
    with pytest.raises(ValueError, match="provider_override requires"):
        mr._validate_model_override(None, "openai-codex")


def test_resolved_task_model_route_prefers_task_overrides_over_profile(kanban_home):
    from hermes_cli import kanban_db_connect as kbc

    profile_dir = kanban_home / "profiles" / "worker"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n  default: gpt-5.6-sol\n"
        "agent:\n  reasoning_effort: medium\n",
        encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        task = kb.get_task(conn, tid)
        provider, model, effort = mr.resolved_task_model_route(task)
        assert (provider, model, effort) == ("openai-codex", "gpt-5.6-sol", "medium")

        overridden = kb.set_route_overrides(
            conn, tid, model="gpt-5.6-terra", provider="openai-codex", reasoning_effort="medium",
        )
        assert overridden
        task = kb.get_task(conn, tid)
        provider, model, effort = mr.resolved_task_model_route(task)
        assert (provider, model, effort) == ("openai-codex", "gpt-5.6-terra", "medium")
