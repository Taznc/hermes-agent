"""Anthropic 401 after an out-of-band OAuth rotation: retry ONCE on the rotated token.

Race being fixed: every Anthropic profile borrows the single shared
``~/.claude/.credentials.json``. Refresh tokens are single-use, so the second any peer
process (another worker, the ``claude`` CLI) rotates that file, the in-flight access
token is revoked and every concurrent request 401s with "OAuth access token has been
revoked" — while a perfectly valid token already sits on disk. Before this fix the
turn died in ``settle_unrecovered_error`` as a "Non-retryable client error".

The fix lives in ``agent.turn_recovery.try_anthropic_rotation_retry`` and is wired into
``agent.turn_api_error.settle_unrecovered_error`` right before the fatal path. These
tests drive that phase with a real temp credentials file (patched where production
reads it: ``agent.anthropic_credentials.claude_code_credentials_path``).
Upstream context: NousResearch/hermes-agent #105797, #92606.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import anthropic_credentials as ac
from agent.error_classifier import classify_api_error
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_retry_state import TurnRetryState
from run_agent import AIAgent

OLD = "sk-ant-oat01-OLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLD"
NEW = "sk-ant-oat01-NEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEW"
API_KEY = "sk-ant-api03-STATICSTATICSTATICSTATICSTATICSTATICSTATIC"


def _revoked_401():
    err = Exception(
        "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', "
        "'message': 'OAuth access token has been revoked.'}}"
    )
    err.status_code = 401
    return err


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    """Real temp ``~/.claude/.credentials.json`` stand-in, patched where production reads it."""
    path = tmp_path / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(ac, "claude_code_credentials_path", lambda: path)
    # Keychain is Darwin-only; make the file the single source regardless of host.
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)

    def write(access_token, *, expires_in_s=8 * 3600):
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": access_token, "refreshToken": "rt-" + access_token[-8:],
            "expiresAt": int((time.time() + expires_in_s) * 1000),
        }}))
        return path

    write(OLD)
    return write


def _make_agent(*, provider="anthropic", api_mode="anthropic_messages", token=OLD):
    """Real ``AIAgent`` instance (mixin methods bound) with only the fields the phase reads."""
    agent = AIAgent.__new__(AIAgent)
    agent.provider = provider
    agent.model = "claude-sonnet-5"
    agent.api_mode = api_mode
    agent.base_url = "https://api.anthropic.com"
    agent.api_key = token
    agent.log_prefix = ""
    agent._anthropic_api_key = token
    agent._anthropic_base_url = "https://api.anthropic.com"
    agent._is_anthropic_oauth = ac._is_oauth_token(token) and provider == "anthropic"
    agent._anthropic_client = MagicMock(name="old_client")
    agent._credential_pool = None
    agent._credential_pool_entry_id = None
    agent._vprint = MagicMock()
    agent._buffer_vprint = MagicMock()
    agent._buffer_status = MagicMock()
    agent._has_pending_fallback = MagicMock(return_value=False)
    agent._try_activate_fallback = MagicMock(return_value=False)
    agent._try_recover_stale_copilot_credential = MagicMock(return_value=False)
    # Terminal-path plumbing (only reached when the fix declines).
    agent._dump_api_request_debug = MagicMock()
    agent._flush_status_buffer = MagicMock()
    agent._summarize_api_error = staticmethod(lambda e: str(e))
    agent._emit_status = MagicMock()
    agent._save_failed_turn = MagicMock()
    return agent


def _settle(agent, retry, messages, *, api_error=None):
    api_error = api_error or _revoked_401()
    classified = classify_api_error(api_error, provider=agent.provider, model=agent.model)
    system = [{"role": "system", "content": "SYSTEM PROMPT (byte-stable)"}]
    api_messages = system + copy.deepcopy(messages)
    with patch("agent.turn_api_error.nonretryable_client_error_result",
               return_value={"failed": True, "final_response": "x", "messages": messages,
                             "completed": False, "error": str(api_error), "api_calls": 1}) as terminal:
        verdict = settle_unrecovered_error(
            agent, api_error=api_error, classified=classified, _retry=retry, status_code=401,
            error_msg=str(api_error), is_context_length_error=False, is_rate_limited=False,
            _is_zai_coding_overload=False, _provider=agent.provider, _base=agent.base_url,
            _model=agent.model, messages=messages, api_messages=api_messages, api_kwargs={},
            active_system_prompt=system[0]["content"], conversation_history=None, approx_tokens=10,
            retry_count=1, max_retries=3, compression_attempts=0, api_call_count=1,
        )
    return verdict, terminal, api_messages, system


def _messages():
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                              "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]


def _built_clients(agent):
    """Patch the wire-client builder so a rebuild is observable without the SDK."""
    built = []

    def _build(token, base_url):
        built.append(token)
        return SimpleNamespace(token=token, close=MagicMock())

    return built, patch.object(agent, "_build_direct_anthropic_client", side_effect=_build)


# ── (a) rotated token on disk → retried once, succeeds, turn continues ──────────────────────


def test_401_with_rotated_token_on_disk_retries_once_on_new_token(cred_file, caplog):
    cred_file(NEW)  # peer process rotated the shared file one second ago
    agent = _make_agent()
    retry = TurnRetryState()
    built, builder = _built_clients(agent)
    with builder, caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        verdict, terminal, _, _ = _settle(agent, retry, _messages())

    assert verdict.action == "continue"
    assert verdict.retry_count == 0
    terminal.assert_not_called()
    assert built == [NEW]                       # client REBUILT, not just a string swap
    assert agent._anthropic_api_key == NEW
    assert agent._anthropic_client.token == NEW
    assert agent._is_anthropic_oauth is True
    assert retry.anthropic_rotation_retry_attempted is True
    assert any("adopted the rotated OAuth token" in r.getMessage() for r in caplog.records)


def test_adopt_path_does_not_spend_refresh_token(cred_file):
    """A rotated token on disk must be ADOPTED, never re-POSTed to the token endpoint."""
    cred_file(NEW)
    agent = _make_agent()
    _, builder = _built_clients(agent)
    with builder, patch.object(ac, "refresh_anthropic_oauth_pure") as post:
        verdict, _, _, _ = _settle(agent, TurnRetryState(), _messages())
    assert verdict.action == "continue"
    post.assert_not_called()


# ── (b) token unchanged → fatal as before ─────────────────────────────────────────────────


def test_401_with_unchanged_token_stays_fatal(cred_file, caplog):
    # File still holds OUR token; a refresh POST would be the only way forward — make it fail
    # like a genuinely revoked grant so we assert the fatal fall-through, not a network call.
    agent = _make_agent()
    built, builder = _built_clients(agent)
    with builder, patch.object(ac, "refresh_anthropic_oauth_pure", side_effect=RuntimeError("invalid_grant")), \
            caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        verdict, terminal, _, _ = _settle(agent, TurnRetryState(), _messages())

    assert verdict.action == "return"
    assert verdict.result["failed"] is True
    terminal.assert_called_once()
    assert built == []
    assert agent._anthropic_api_key == OLD
    assert any("rotation-retry exhausted" in r.getMessage() for r in caplog.records)


# ── (c) 401 twice → fatal after exactly one retry ─────────────────────────────────────────


def test_second_401_in_same_iteration_is_fatal_after_one_retry(cred_file):
    cred_file(NEW)
    agent = _make_agent()
    retry = TurnRetryState()
    built, builder = _built_clients(agent)
    with builder, patch.object(ac, "refresh_anthropic_oauth_pure") as post:
        first, terminal1, _, _ = _settle(agent, retry, _messages())
        assert first.action == "continue"
        terminal1.assert_not_called()
        # The retried request 401s again (e.g. NEW got revoked too). Same retry state → no loop.
        cred_file("sk-ant-oat01-EVENNEWEREVENNEWEREVENNEWEREVENNEWEREVENNEWER")
        second, terminal2, _, _ = _settle(agent, retry, _messages())

    assert second.action == "return"
    terminal2.assert_called_once()
    assert built == [NEW]                       # exactly one rebuild, never a second
    assert agent._anthropic_api_key == NEW
    post.assert_not_called()


# ── (d) non-anthropic / non-OAuth 401 → fatal, no credential re-resolution ────────────────


@pytest.mark.parametrize("provider,api_mode,token", [
    ("openrouter", "chat_completions", "sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxx"),
    ("minimax", "anthropic_messages", "sk-ant-oat01-THIRDPARTYTHIRDPARTYTHIRDPARTY"),
    ("anthropic", "anthropic_messages", API_KEY),        # static API key: never rotates
])
def test_401_outside_anthropic_oauth_is_fatal_without_touching_credentials(cred_file, provider, api_mode, token):
    cred_file(NEW)  # a rotated token exists, but it is not ours to adopt
    agent = _make_agent(provider=provider, api_mode=api_mode, token=token)
    retry = TurnRetryState()
    built, builder = _built_clients(agent)
    with builder, patch.object(ac, "read_claude_code_credentials") as read, \
            patch.object(ac, "_refresh_oauth_token") as refresh:
        verdict, terminal, _, _ = _settle(agent, retry, _messages())

    assert verdict.action == "return"
    terminal.assert_called_once()
    read.assert_not_called()
    refresh.assert_not_called()
    assert built == []
    assert agent._anthropic_api_key == token
    assert agent.api_key == token


# ── (e) the retry must not alter messages / system prompt ─────────────────────────────────


def test_rotation_retry_leaves_messages_and_system_prompt_untouched(cred_file):
    cred_file(NEW)
    agent = _make_agent()
    messages = _messages()
    before = copy.deepcopy(messages)
    _, builder = _built_clients(agent)
    with builder:
        verdict, _, api_messages, system = _settle(agent, TurnRetryState(), messages)

    assert verdict.action == "continue"
    assert messages == before                                     # canonical history byte-identical
    assert api_messages[0] == system[0]                           # system prompt untouched
    assert api_messages[1:] == before                             # per-call copy untouched
    assert verdict.active_system_prompt == system[0]["content"]
    roles = [m["role"] for m in messages]
    assert all(a != b for a, b in zip(roles, roles[1:]))          # strict alternation preserved


# ── pool-owned entry (hermes_pkce): re-resolves through the pool, not the borrowed file ───


def test_pool_owned_oauth_entry_reresolves_via_pool(cred_file):
    agent = _make_agent()
    entry = SimpleNamespace(id="25bf7b", source="hermes_pkce", auth_type="oauth",
                            access_token=OLD, expires_at_ms=int(time.time() * 1000) + 3_600_000)
    rotated = SimpleNamespace(**{**vars(entry), "access_token": NEW})
    agent._credential_pool = SimpleNamespace(entries=lambda: [entry])
    agent._credential_pool_entry_id = "25bf7b"
    built, builder = _built_clients(agent)
    fresh_pool = SimpleNamespace(entries=lambda: [rotated])
    with builder, patch("agent.credential_pool.load_pool", return_value=fresh_pool), \
            patch.object(ac, "read_claude_code_credentials") as read_file:
        verdict, _, _, _ = _settle(agent, TurnRetryState(), _messages())

    assert verdict.action == "continue"
    assert built == [NEW]
    read_file.assert_not_called()               # pool-owned rows never touch ~/.claude
