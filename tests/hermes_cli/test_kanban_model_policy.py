from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PIN_HOME", raising=False)
    kb.init_db()
    with kbc.connect_closing() as opened:
        yield opened


def _write_profile(home, name, model="gpt-5.6-sol", effort="medium"):
    profile = home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "profile.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    (profile / "config.yaml").write_text(
        f"model:\n  provider: openai-codex\n  default: {model}\n"
        f"agent:\n  reasoning_effort: {effort}\n",
        encoding="utf-8",
    )


def test_policy_matrix_accepts_only_approved_unattended_routes():
    for provider, model, effort in (
        ("openai-codex", "gpt-5.6-luna", "low"),
        ("openai-codex", "gpt-5.6-terra", "medium"),
        ("openai-codex", "gpt-5.6-sol", "medium"),
        ("anthropic", "claude-sonnet-5", "high"),
        ("anthropic", "claude-opus-5", "high"),
    ):
        decision = kb.validate_model_effort_policy(
            provider=provider, model=model, reasoning_effort=effort,
            assignee="worker", policy={},
        )
        assert decision.forced is False

    with pytest.raises(ValueError, match="Luna.*low"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-luna", reasoning_effort="medium",
            assignee="worker", policy={},
        )
    with pytest.raises(ValueError, match="unknown model route"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-7-future", reasoning_effort="medium",
            assignee="worker", policy={},
        )


def test_claude_baseline_routes_are_unattended_approved_not_operator_forced():
    """2026-09-15 bootstrap: Claude promoted to primary unattended provider.

    RED on the pre-fix policy (only openai-codex routes were pre-approved;
    anthropic/high was rejected outright and could not even be force-approved,
    since ``forceable_model`` gated force-approval to ``provider ==
    "openai-codex"``). GREEN once claude-sonnet-5/high and claude-opus-5/high
    join ``_DEFAULT_UNATTENDED_ROUTES`` and the operator-only-effort veto
    exempts exactly those two pre-approved triples.
    """
    for model in ("claude-sonnet-5", "claude-opus-5"):
        decision = kb.validate_model_effort_policy(
            provider="anthropic", model=model, reasoning_effort="high",
            assignee="claudecode", policy={},
        )
        assert decision.forced is False

    # A route that merely resembles the baseline (different effort) must stay
    # denied — the exemption is scoped to the exact pre-approved triples, not
    # to "anthropic at any effort".
    with pytest.raises(ValueError, match="unattended route"):
        kb.validate_model_effort_policy(
            provider="anthropic", model="claude-sonnet-5", reasoning_effort="xhigh",
            assignee="claudecode", policy={},
        )
    with pytest.raises(ValueError, match="unknown model route"):
        kb.validate_model_effort_policy(
            provider="anthropic", model="claude-sonnet-5", reasoning_effort="medium",
            assignee="claudecode", policy={},
        )

    # Existing Codex routes and every other operator-only-effort/Astra
    # rejection are unchanged by the exemption.
    with pytest.raises(ValueError, match="unattended route"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="high",
            assignee="claudecode", policy={},
        )
    with pytest.raises(ValueError, match="unattended route"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-6-astra", reasoning_effort="medium",
            assignee="claudecode", policy={},
        )


@pytest.mark.parametrize("model", [
    "gpt-6-astra", "gpt-6-astra-pro", "gpt-5.4-mini",
    "gpt-5.3-codex-spark", "openai/gpt-oss-120b:free",
])
def test_policy_denies_unattended_escape_hatch_models(model):
    with pytest.raises(ValueError, match="model policy"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model=model, reasoning_effort="medium",
            assignee="worker", policy={},
        )


@pytest.mark.parametrize("model", [
    "gpt-5.4-mini", "gpt-5.3-codex-spark", "openai/gpt-oss-120b:free",
])
def test_force_cannot_admit_categorically_denied_models(model):
    with pytest.raises(ValueError, match="cannot be force-approved"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model=model, reasoning_effort="medium",
            assignee="worker", policy={}, force=True,
            force_reason="operator exception", forced_by="operator",
        )


def test_luna_is_refused_for_independent_review_profiles_without_force():
    with pytest.raises(ValueError, match="mechanical-only.*reviewer"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-luna", reasoning_effort="low",
            assignee="reviewer", policy={},
        )


def test_force_requires_reason_and_binds_to_profile_and_route():
    with pytest.raises(ValueError, match="non-empty reason"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-6-astra", reasoning_effort="high",
            assignee="reviewer", policy={}, force=True, forced_by="reviewer",
        )
    with pytest.raises(ValueError, match="could not resolve"):
        kb.validate_model_effort_policy(
            provider=None, model=None, reasoning_effort=None, assignee="reviewer",
            force=True, force_reason="incident 42", forced_by="operator",
        )

    decision = kb.validate_model_effort_policy(
        provider="openai-codex", model="gpt-6-astra", reasoning_effort="high",
        assignee="reviewer", policy={}, force=True, force_reason="incident 42",
        forced_by="reviewer",
    )
    assert decision.forced is True
    assert decision.force_reason == "incident 42"
    assert json.loads(decision.force_route) == {
        "assignee": "reviewer", "model": "gpt-6-astra",
        "provider": "openai-codex", "reasoning_effort": "high",
    }
    with pytest.raises(ValueError, match="cannot force-approve unknown route"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-7-future", reasoning_effort="medium",
            assignee="reviewer", policy={}, force=True,
            force_reason="unreviewed future model", forced_by="operator",
        )
    with pytest.raises(ValueError, match="cannot force-approve unknown route"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="not-astra-future", reasoning_effort="high",
            assignee="reviewer", policy={}, force=True,
            force_reason="substring is not a known family", forced_by="operator",
        )
    with pytest.raises(ValueError, match="cannot force-approve unknown route"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-terra", reasoning_effort="low",
            assignee="worker", policy={}, force=True,
            force_reason="invalid effort pairing", forced_by="operator",
        )


def test_create_rejects_goal_mode_and_persists_force_provenance(conn):
    with pytest.raises(ValueError, match="goal_mode is disabled"):
        kb.create_task(conn, title="loop", assignee="reviewer", goal_mode=True)

    tid = kb.create_task(
        conn, title="forced", assignee="reviewer",
        model_override="gpt-6-astra", provider_override="openai-codex",
        reasoning_effort="high", policy_force=True,
        policy_force_reason="security incident", policy_forced_by="reviewer",
    )
    task = kb.get_task(conn, tid)
    assert task.policy_forced_by == "reviewer"
    assert task.policy_force_reason == "security incident"
    assert json.loads(task.policy_force_route)["model"] == "gpt-6-astra"
    created = next(e for e in kb.list_events(conn, tid) if e.kind == "created")
    assert created.payload["model_policy_force"]["reason"] == "security incident"


@pytest.mark.parametrize(
    ("assignee", "model"),
    [("missing-profile", "gpt-6-astra"), (None, "future-expensive-model")],
)
def test_create_rejects_explicit_denied_or_unknown_route_without_profile_config(
    conn, assignee, model,
):
    with pytest.raises(ValueError, match="model policy"):
        kb.create_task(
            conn, title="explicit route", assignee=assignee,
            model_override=model, provider_override="openai-codex",
            reasoning_effort="medium",
        )


def test_mutations_validate_combined_route_and_clear_force_on_change(conn):
    home = kb.kanban_home()
    _write_profile(home, "worker")
    _write_profile(home, "reviewer")
    tid = kb.create_task(conn, title="normal", assignee="worker")

    with pytest.raises(ValueError, match="operator force"):
        kb.set_reasoning_effort(conn, tid, "high")
    assert kb.set_reasoning_effort(
        conn, tid, "high", policy_force=True,
        policy_force_reason="hard security diagnosis", policy_forced_by="operator",
    )
    forced = kb.get_task(conn, tid)
    assert forced is not None
    assert forced.policy_force_reason == "hard security diagnosis"

    # A profile handoff cannot inherit that approval silently.
    with pytest.raises(ValueError, match="operator force"):
        kb.assign_task(conn, tid, "reviewer")
    unchanged = kb.get_task(conn, tid)
    assert unchanged is not None
    assert unchanged.assignee == "worker"

    # Moving back to an approved exact route clears exception provenance.
    assert kb.set_reasoning_effort(conn, tid, "medium")
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.policy_forced_by is None
    assert task.policy_force_reason is None
    assert task.policy_force_route is None


def test_cli_force_without_profile_flag_records_default_actor(conn, monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "agent.delegation_context.is_dispatcher_owned_worker_context", lambda: False,
    )
    _write_profile(kb.kanban_home(), "worker")

    out = kc.run_slash(
        'create "forced" --assignee worker --reasoning high --policy-force '
        '--policy-force-reason "operator approved"'
    )

    assert out.startswith("Created "), out
    task = kb.list_tasks(conn, assignee="worker")[0]
    assert task.policy_forced_by == "default"


def test_cli_task_dict_exposes_durable_force_provenance(conn):
    from hermes_cli.kanban_output import _task_to_dict

    tid = kb.create_task(
        conn, title="forced output", assignee="reviewer",
        model_override="gpt-6-astra", provider_override="openai-codex",
        reasoning_effort="high", policy_force=True,
        policy_force_reason="security incident", policy_forced_by="operator",
    )

    task = kb.get_task(conn, tid)
    assert task is not None
    payload = _task_to_dict(task)
    assert payload["policy_forced_by"] == "operator"
    assert payload["policy_force_reason"] == "security incident"
    assert json.loads(payload["policy_force_route"])["assignee"] == "reviewer"


def test_board_policy_can_only_restrict_builtin_routes(conn):
    kb.write_board_metadata(
        "default",
        model_policy={
            "allowed_routes": [{
                "provider": "openai-codex", "model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
            }],
        },
    )
    policy = kb._effective_model_policy("worker", "default")
    decision = kb.validate_model_effort_policy(
        provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="medium",
        assignee="worker", policy=policy,
    )
    assert decision.forced is False
    with pytest.raises(ValueError, match="refuses"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-terra", reasoning_effort="medium",
            assignee="worker", policy=policy,
        )
    with pytest.raises(ValueError, match="unsupported route"):
        kb.validate_model_effort_policy(
            provider="vendor", model="future-1", reasoning_effort="medium",
            assignee="worker", policy={"allowed_routes": [{
                "provider": "vendor", "model": "future-1", "reasoning_effort": "medium",
            }]},
        )


def test_hidden_fallbacks_and_moa_are_never_forceable():
    with pytest.raises(ValueError, match="hidden fallback routes"):
        kb.validate_model_effort_policy(
            provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="medium",
            assignee="worker", force=True,
            force_reason="temporary approval", forced_by="operator",
            policy={"_fallback_routes": [{"provider": "x", "model": "cheap"}]},
        )
    with pytest.raises(ValueError, match="MoA"):
        kb.validate_model_effort_policy(
            provider="moa", model="expensive-preset", reasoning_effort="medium",
            assignee="worker", force=True,
            force_reason="temporary approval", forced_by="operator",
        )


def test_kanban_worker_cannot_bypass_policy_with_delegation(monkeypatch):
    from tools.delegate_tool import delegate_task

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_policy")
    payload = json.loads(delegate_task(goal="use Astra", parent_agent=object()))
    assert "disabled inside Kanban workers" in payload["error"]


def test_default_assignee_handoff_refuses_disallowed_profile_route(conn):
    _write_profile(kb.kanban_home(), "reviewer", model="gpt-6-astra")
    tid = kb.create_task(conn, title="unassigned")

    assert not kbd._apply_default_assignee(conn, tid, "reviewer", dry_run=False)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee is None


def test_default_reviewer_handoff_refuses_disallowed_profile_route(conn):
    home = kb.kanban_home()
    _write_profile(home, "worker")
    _write_profile(home, "reviewer", model="gpt-6-astra")
    tid = kb.create_task(conn, title="review me", assignee="worker")
    assert kb.request_review(conn, tid, summary="ready")

    assert not kbd._apply_default_reviewer(
        conn, tid, "reviewer", previous_assignee="worker", dry_run=False,
    )
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "review"
    assert task.assignee == "worker"


def test_explicit_review_handoff_refuses_luna_for_any_reviewer_profile(conn):
    home = kb.kanban_home()
    _write_profile(home, "worker")
    _write_profile(home, "codexreview", model="gpt-5.6-luna", effort="low")
    tid = kb.create_task(conn, title="review me", assignee="worker")

    result = kb.request_review(
        conn, tid, reviewer="codexreview", summary="ready", with_reason=True,
    )
    assert isinstance(result, tuple)
    ok, reason = result
    assert ok is False
    assert isinstance(reason, str)
    assert "mechanical-only Luna" in reason
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "worker"


def test_forced_implementer_route_survives_review_rework_only_for_implementer(conn):
    home = kb.kanban_home()
    _write_profile(home, "builder")
    _write_profile(home, "reviewer")
    tid = kb.create_task(
        conn, title="forced implementation", assignee="builder",
        model_override="gpt-6-astra", provider_override="openai-codex",
        reasoning_effort="high", policy_force=True,
        policy_force_reason="hard security incident", policy_forced_by="operator",
    )
    implementation = kb.claim_task(conn, tid, claimer="builder:1")
    assert implementation is not None

    assert kb.request_review(
        conn, tid, reviewer="reviewer", expected_run_id=implementation.current_run_id,
    )
    review_lane = kb.get_task(conn, tid)
    assert review_lane is not None
    assert review_lane.assignee == "reviewer"
    assert review_lane.policy_forced_by is None
    assert review_lane.model_override is None

    review = kb.claim_review_task(conn, tid, claimer="reviewer:1")
    assert review is not None
    assert kb.request_changes(
        conn, tid, reason="add regression", expected_run_id=review.current_run_id,
        blockers=[{"basis": "original_ac", "reference": "test acceptance contract"}],
    ) == (True, "builder")

    returned = kb.get_task(conn, tid)
    assert returned is not None
    assert returned.status == "ready"
    assert returned.assignee == "builder"
    assert returned.model_override == "gpt-6-astra"
    assert returned.reasoning_effort == "high"
    assert returned.policy_forced_by == "operator"
    assert returned.policy_force_reason == "hard security incident"


def test_review_reopen_restores_forced_implementer_route(conn):
    home = kb.kanban_home()
    _write_profile(home, "builder")
    _write_profile(home, "reviewer")
    tid = kb.create_task(
        conn, title="forced reopen", assignee="builder",
        model_override="gpt-6-astra", provider_override="openai-codex",
        reasoning_effort="high", policy_force=True,
        policy_force_reason="hard security incident", policy_forced_by="operator",
    )
    implementation = kb.claim_task(conn, tid, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn, tid, reviewer="reviewer", expected_run_id=implementation.current_run_id,
    ) is True

    assert kb.reopen_review_task(conn, tid) is True
    returned = kb.get_task(conn, tid)
    assert returned is not None
    assert returned.assignee == "builder"
    assert returned.model_override == "gpt-6-astra"
    assert returned.provider_override == "openai-codex"
    assert returned.reasoning_effort == "high"
    assert returned.policy_forced_by == "operator"
    assert returned.policy_force_reason == "hard security incident"
    assert returned.policy_force_route is not None


def test_dispatch_revalidates_tampered_legacy_row_before_spawn(conn):
    home = kb.kanban_home()
    _write_profile(home, "worker")
    tid = kb.create_task(conn, title="approved", assignee="worker")
    # Simulate a stale/imported/direct-SQL row that bypassed mutation checks.
    conn.execute(
        "UPDATE tasks SET model_override = 'gpt-6-astra', provider_override = 'openai-codex' "
        "WHERE id = ?", (tid,),
    )
    conn.commit()
    spawned = []
    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *args, **kwargs: spawned.append(args) or 12345,
        max_spawn=1,
    )
    assert spawned == []
    assert result.model_policy_blocked[0][0] == tid
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "blocked"
    assert task.current_run_id is None


def test_cli_unknown_route_diagnostic_offers_only_supported_remediation(conn, capsys):
    _write_profile(kb.kanban_home(), "worker")
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "kanban", "create", "unknown route", "--assignee", "worker",
        "--model", "gpt-7-future", "--provider", "openai-codex",
        "--reasoning", "medium",
    ])

    assert kc.kanban_command(args) == 2
    diagnostic = capsys.readouterr().err
    assert "unknown model route" in diagnostic
    assert "choose an approved exact route" in diagnostic
    assert "restrict but cannot expand" in diagnostic
    assert "use operator force" not in diagnostic
    assert "add an exact allowed_routes entry" not in diagnostic
    assert kb.list_tasks(conn) == []


def test_cli_requires_force_reason_for_denied_route(conn, monkeypatch, capsys):
    from hermes_cli import kanban as kanban_cli

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    _write_profile(kb.kanban_home(), "worker")
    monkeypatch.setenv("HERMES_PROFILE", "operator")
    parser = argparse.ArgumentParser()
    kanban_cli.build_parser(parser.add_subparsers(dest="command"))
    base = [
        "kanban", "create", "cli policy", "--assignee", "worker",
        "--model", "gpt-6-astra", "--provider", "openai-codex",
        "--reasoning", "medium",
    ]
    assert kanban_cli.kanban_command(parser.parse_args(base)) == 2
    assert "operator force" in capsys.readouterr().err
    from agent.delegation_context import non_dispatcher_owned_context
    with non_dispatcher_owned_context():
        assert kanban_cli.kanban_command(parser.parse_args([
            *base, "--policy-force", "--policy-force-reason", "incident response",
        ])) == 0
    capsys.readouterr()
