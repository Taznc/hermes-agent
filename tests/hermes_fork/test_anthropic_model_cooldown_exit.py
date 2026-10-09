"""A key the Anthropic pool benched for ONE model is a quota wall, not a credential failure (t_021144ff).

``runtime_provider._anthropic_token_or_raise`` raises an ``AuthError`` when the pool has benched every
key for the requested model but another model still resolves. Untagged, ``is_rate_limited_auth_error``
missed it, so a Kanban worker exited 1 (booked ``crashed``, card ``gave_up`` after two attempts)
instead of ``KANBAN_RATE_LIMIT_EXIT_CODE`` (booked ``rate_limited``, requeued without a failure).
These tests drive the real raise site through the real CLI flag and exit-code mapping.
"""

from __future__ import annotations

import pytest

from hermes_cli import runtime_provider
from hermes_cli.auth import AuthError, format_auth_error, is_rate_limited_auth_error, primary_failure_wording
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_single_query import _single_query_exit_code
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

MODEL = "claude-opus-5-5"


def _pool(monkeypatch, *, benched_model: str | None):
    """``resolve_anthropic_token``: None for *benched_model* (and for every model when it is None)."""
    def resolve(*, model=None):
        if benched_model is None:
            return None
        return None if model == benched_model else "sk-ant-still-good"
    monkeypatch.setattr("agent.anthropic_credentials.resolve_anthropic_token", resolve)


def _raised(monkeypatch, *, benched_model: str | None) -> AuthError:
    _pool(monkeypatch, benched_model=benched_model)
    with pytest.raises(AuthError) as exc:
        runtime_provider._anthropic_token_or_raise(model=MODEL)
    return exc.value


def _shell(monkeypatch, tmp_path, *, fallback=()):
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    printed: list[str] = []
    monkeypatch.setattr("cli._cprint", printed.append, raising=False)

    def resolve_runtime_provider(**kw):
        if kw.get("requested") == "anthropic":
            runtime_provider._anthropic_token_or_raise(model=kw.get("target_model"))
        return {"provider": "custom", "base_url": "http://x/v1", "api_key": "k"}

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve_runtime_provider)
    monkeypatch.setattr("hermes_cli.fallback_config.resolve_entry_api_key", lambda entry: "k")
    shell = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
    shell.model = MODEL
    shell.requested_provider = "anthropic"
    shell._explicit_api_key = None
    shell._explicit_base_url = None
    shell._fallback_model = list(fallback)
    shell.tool_progress_mode = "off"
    return shell, printed


def test_model_cooldown_error_is_tagged_rate_limited_at_its_raise_site(monkeypatch):
    err = _raised(monkeypatch, benched_model=MODEL)
    assert err.code == runtime_provider.CODEX_RATE_LIMITED_CODE
    assert err.relogin_required is False
    assert err.retryable is True
    assert is_rate_limited_auth_error(err)


def test_kanban_worker_exits_tempfail_on_model_cooldown(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    _pool(monkeypatch, benched_model=MODEL)
    shell, _ = _shell(monkeypatch, tmp_path)

    assert shell._ensure_runtime_credentials() is False
    assert shell._credentials_rate_limited is True
    assert _single_query_exit_code(None, credentials_rate_limited=shell._credentials_rate_limited) == (
        KANBAN_RATE_LIMIT_EXIT_CODE)
    # The user still learns the cooldown is model-scoped, and is not told to re-authenticate.
    err = capsys.readouterr().err
    assert f"rate-limited for {MODEL}" in err and "other Claude models remain available" in err
    assert "re-authenticate" not in err


def test_missing_anthropic_credentials_still_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    err = _raised(monkeypatch, benched_model=None)
    assert not is_rate_limited_auth_error(err)

    shell, _ = _shell(monkeypatch, tmp_path)
    assert shell._ensure_runtime_credentials() is False
    assert shell._credentials_rate_limited is False
    assert _single_query_exit_code(None, credentials_rate_limited=shell._credentials_rate_limited) == 1


def test_model_cooldown_reads_as_quota_not_auth(monkeypatch, tmp_path):
    err = _raised(monkeypatch, benched_model=MODEL)
    assert primary_failure_wording(err) == ("rate-limited (429)", "Primary provider quota exhausted")
    assert format_auth_error(err) == str(err)
    assert "other Claude models remain available" in format_auth_error(err)

    # The CLI fallback surface prints the quota wording when it swaps providers.
    shell, printed = _shell(monkeypatch, tmp_path, fallback=[{"provider": "custom", "model": "local-model"}])
    assert shell._resolve_fallback_runtime(err) is not None
    assert "Primary provider quota exhausted" in printed[-1]
    assert "auth failed" not in printed[-1]
