"""Unattended Kanban route policy (hermes_fork/kanban/route_policy.py) and its
dispatcher anchor ``kanban-unattended-route-policy``.

Every test runs a real ``dispatch_once`` against an isolated HERMES_HOME with
real profile directories; only the final worker process launch
(``_default_spawn``) is replaced by a recorder, so the gate is exercised
through the same claim -> admit -> spawn path production uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import route_policy as rp

APPROVED_PROFILE = """\
model:
  default: claude-opus-5-5
  provider: anthropic
  fallback_providers: []
agent:
  reasoning_effort: high
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return h


@pytest.fixture
def spawned(monkeypatch):
    calls: list[str] = []

    def _record(task, workspace, *, board=None):
        calls.append(task.id)
        return None  # no PID: nothing is launched

    monkeypatch.setattr(kbd, "_default_spawn", _record)
    return calls


def _profile(home: Path, name: str, config: str) -> None:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(config, encoding="utf-8")


def _dispatch(conn):
    return kbd.dispatch_once(conn)


def _block_reason(conn, tid: str) -> str:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row is not None, "no blocked event recorded"
    return row["payload"]


def _assert_denied(conn, res, spawned, tid: str, *needles: str) -> None:
    task = kb.get_task(conn, tid)
    assert tid not in spawned, "denied route must never reach the spawner"
    assert tid in res.auto_blocked
    assert tid not in [s[0] for s in res.spawned]
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.claim_lock is None
    reason = _block_reason(conn, tid)
    assert "kanban unattended route policy" in reason
    for needle in needles:
        assert needle in reason, reason
    # Sticky: the next tick neither resumes nor spawns it.
    res2 = _dispatch(conn)
    assert kb.get_task(conn, tid).status == "blocked"
    assert tid not in spawned and tid not in [s[0] for s in res2.spawned]


# -- approved -------------------------------------------------------------------


def test_approved_profile_route_spawns(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ok", assignee="alpha")
        res = _dispatch(conn)
        assert spawned == [tid]
        assert tid in [s[0] for s in res.spawned]
        assert kb.get_task(conn, tid).status == "running"


def test_approved_per_model_reasoning_override_is_what_counts(home, spawned):
    """``agent.reasoning_overrides`` for the model beats ``agent.reasoning_effort``
    (same resolver the worker uses), so the effective route is the one judged."""
    _profile(home, "alpha", APPROVED_PROFILE.replace(
        "  reasoning_effort: high\n",
        "  reasoning_effort: low\n  reasoning_overrides:\n    claude-opus-5-5: high\n",
    ))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ok", assignee="alpha")
        _dispatch(conn)
        assert spawned == [tid]


def test_review_lane_is_gated_too(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE)
    _profile(home, "rev", APPROVED_PROFILE.replace("claude-opus-5-5", "claude-opus-4"))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="alpha")
        _dispatch(conn)
        assert spawned == [tid]
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary="done", reviewer="rev", expected_run_id=run_id)
        res = _dispatch(conn)
        assert spawned == [tid]  # reviewer route not approved
        _assert_denied(conn, res, spawned[1:], tid, "anthropic/claude-opus-4/high")


# -- explicit task override -----------------------------------------------------


def test_explicit_approved_override_spawns(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE)
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="codex", assignee="alpha",
            model_override="gpt-6-sol", provider_override="openai-codex", reasoning_effort="medium",
        )
        _dispatch(conn)
        assert spawned == [tid]


def test_explicit_override_with_unapproved_effort_is_denied(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="xhigh", assignee="alpha", reasoning_effort="xhigh")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "primary anthropic/claude-opus-5-5/xhigh", "not an approved")


def test_explicit_model_override_without_provider_uses_profile_provider(home, spawned):
    """``-m`` alone resolves against the profile's provider — the gate judges that pair."""
    _profile(home, "alpha", APPROVED_PROFILE)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="mismatch", assignee="alpha", model_override="gpt-6-sol")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "primary anthropic/gpt-6-sol/high")


# -- unconfigured -----------------------------------------------------------------


@pytest.mark.parametrize("config, needle", [
    ("{}\n", "not fully resolved"),  # no model at all
    ("model:\n  default: claude-opus-5-5\nagent:\n  reasoning_effort: high\n", "not fully resolved"),  # no provider
    ("model:\n  default: claude-opus-5-5\n  provider: auto\nagent:\n  reasoning_effort: high\n", "not fully resolved"),
    ("model:\n  default: claude-opus-5-5\n  provider: anthropic\n", "not fully resolved"),  # no effort
])
def test_unconfigured_profile_route_fails_closed(home, spawned, config, needle):
    _profile(home, "alpha", config)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bare", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, needle)


def test_unreadable_profile_config_fails_closed(home, spawned):
    _profile(home, "alpha", "model: [unterminated\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="broken yaml", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "could not be resolved")


# -- fallbacks ----------------------------------------------------------------------


def test_approved_fallback_chain_spawns(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE.replace(
        "  fallback_providers: []\n",
        "  fallback_providers:\n  - provider: anthropic\n    model: claude-sonnet-5\n",
    ))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fb ok", assignee="alpha")
        _dispatch(conn)
        assert spawned == [tid]


@pytest.mark.parametrize("fallback_yaml", [
    # root-level chain (what the live default profile carries)
    "fallback_providers:\n- provider: openai-codex\n  model: gpt-6-sol\n",
    # model-nested chain
    "model:\n  fallback_providers:\n  - provider: openai-codex\n    model: gpt-6-sol\n",
    # legacy single-dict key
    "fallback_model:\n  provider: openai-codex\n  model: gpt-6-sol\n",
])
def test_unapproved_fallback_is_denied_even_with_approved_primary(home, spawned, fallback_yaml):
    """gpt-6-sol is approved only at ``medium``; the fallback re-resolves the
    profile's ``high``, so the fallback route is unapproved and admission fails
    before a primary outage could silently switch to it."""
    base = APPROVED_PROFILE.replace("  fallback_providers: []\n", "")
    if fallback_yaml.startswith("model:"):
        config = base.replace("  provider: anthropic\n", "  provider: anthropic\n" + fallback_yaml.split("\n", 1)[1])
    else:
        config = base + fallback_yaml
    _profile(home, "alpha", config)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fb bad", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "fallback[0] openai-codex/gpt-6-sol/high")


def test_task_reasoning_override_does_not_launder_fallback(home, spawned):
    """``--reasoning`` pins only the primary; the fallback swap re-resolves effort
    from config, so a medium card with a Codex fallback on a high profile is denied."""
    _profile(home, "alpha", APPROVED_PROFILE.replace(
        "  fallback_providers: []\n",
        "  fallback_providers:\n  - provider: openai-codex\n    model: gpt-6-sol\n",
    ))
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="fb launder", assignee="alpha",
            model_override="gpt-6-sol", provider_override="openai-codex", reasoning_effort="medium",
        )
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "fallback[0] openai-codex/gpt-6-sol/high")


# -- categorical denials ---------------------------------------------------------------


@pytest.mark.parametrize("provider, model, needle", [
    ("nous", "hermes-4-405b", "Nous Portal"),
    ("nous-portal", "claude-opus-5-5", "Nous Portal"),
    ("openrouter", "meta-llama/llama-4:free", "'free'"),
    ("openrouter", "vendor/free", "'free'"),
    ("openai-codex", "gpt-5.6-mini", "'mini'"),
    ("openai-codex", "gpt-5.6-codex-spark", "'spark'"),
])
def test_free_and_categorical_routes_denied_as_primary(home, spawned, provider, model, needle):
    _profile(home, "alpha", f"model:\n  default: {model}\n  provider: {provider}\nagent:\n  reasoning_effort: medium\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="free", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, needle)


def test_nous_free_tier_denied_as_fallback(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE.replace(
        "  fallback_providers: []\n",
        "  fallback_providers:\n  - provider: nous\n    model: claude-opus-5-5\n",
    ))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="nous fb", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "fallback[0] nous/claude-opus-5-5/high", "Nous Portal")


def test_explicit_free_override_denied(home, spawned):
    _profile(home, "alpha", APPROVED_PROFILE)
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="free override", assignee="alpha",
            model_override="deepseek/deepseek-r1:free", provider_override="openrouter", reasoning_effort="high",
        )
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "'free'")


def test_categorical_denial_precedes_allowlist(monkeypatch):
    """Even if someone adds a Nous/free triple to the allowlist, it stays denied."""
    monkeypatch.setattr(rp, "APPROVED_UNATTENDED_ROUTES", rp.APPROVED_UNATTENDED_ROUTES | {
        ("nous", "claude-opus-5-5", "high"), ("openrouter", "x:free", "high"),
    })
    assert "Nous Portal" in rp.route_denial(rp.Route("primary", "nous", "claude-opus-5-5", "high"))
    assert "'free'" in rp.route_denial(rp.Route("primary", "openrouter", "x:free", "high"))


def test_free_marker_is_whole_token_only():
    """A substring like 'freeform' or 'minimax' is not a marker; the allowlist still decides."""
    assert "not an approved" in rp.route_denial(rp.Route("primary", "custom", "freeform-7b", "high"))
    assert "not an approved" in rp.route_denial(rp.Route("primary", "minimax", "minimax-m2", "high"))


# -- recovery ------------------------------------------------------------------------------


def test_operator_fix_then_unblock_spawns(home, spawned):
    _profile(home, "alpha", "model:\n  default: gpt-5.6-mini\n  provider: openai-codex\nagent:\n  reasoning_effort: low\n")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix me", assignee="alpha")
        _dispatch(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        _profile(home, "alpha", APPROVED_PROFILE)
        assert kb.unblock_task(conn, tid)
        _dispatch(conn)
        assert spawned == [tid]


def test_injected_spawn_fn_is_gated_too(home):
    """An injected ``spawn_fn`` still launches an unattended worker, so the
    gate runs before it exactly as before the default spawner."""
    _profile(home, "alpha", "{}\n")
    _profile(home, "beta", APPROVED_PROFILE)
    got: list[str] = []
    with kbc.connect() as conn:
        bad = kb.create_task(conn, title="injected bad", assignee="alpha")
        ok = kb.create_task(conn, title="injected ok", assignee="beta")
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws: got.append(task.id))
        assert got == [ok]
        assert bad in res.auto_blocked
        task = kb.get_task(conn, bad)
        assert task.status == "blocked" and task.block_kind == "needs_input"
        assert "not fully resolved" in _block_reason(conn, bad)


def test_fork_suite_runs_with_gate_live():
    """The repo-root conftest waives admission only outside tests/hermes_fork."""
    assert rp._ADMIT_ALL_FOR_TESTS is False


# -- endpoint / credential redirection --------------------------------------------------


@pytest.mark.parametrize("override", [
    "base_url: https://evil.example/v1",
    "api_key: sk-other",
    "key_env: OTHER_KEY",
    "api_key_env: OTHER_KEY",
])
def test_approved_fallback_redirected_to_other_endpoint_is_denied(home, spawned, override):
    """``resolve_runtime_with_fallback`` forwards a fallback entry's base_url /
    key into ``resolve_runtime_provider``; an approved (provider, model, effort)
    triple pointed at another endpoint or credential is not the approved route."""
    _profile(home, "alpha", APPROVED_PROFILE.replace(
        "  fallback_providers: []\n",
        f"  fallback_providers:\n  - provider: anthropic\n    model: claude-opus-5-5\n    {override}\n",
    ))
    key = override.split(":", 1)[0]
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fb redirect", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "fallback[0] anthropic/claude-opus-5-5/high", key)


@pytest.mark.parametrize("override", ["base_url: https://proxy.example/v1", "api_key: sk-other"])
def test_approved_primary_redirected_to_other_endpoint_is_denied(home, spawned, override):
    _profile(home, "alpha", APPROVED_PROFILE.replace("  provider: anthropic\n", f"  provider: anthropic\n  {override}\n"))
    key = override.split(":", 1)[0]
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="primary redirect", assignee="alpha")
        res = _dispatch(conn)
        _assert_denied(conn, res, spawned, tid, "primary anthropic/claude-opus-5-5/high", f"model.{key}")


def test_empty_endpoint_keys_are_not_overrides(home, spawned):
    """``base_url: ''`` (what `hermes setup` writes) redirects nothing."""
    _profile(home, "alpha", APPROVED_PROFILE.replace("  provider: anthropic\n", "  provider: anthropic\n  base_url: ''\n"))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="empty base_url", assignee="alpha")
        _dispatch(conn)
        assert spawned == [tid]
