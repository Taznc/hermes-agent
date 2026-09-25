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
import hashlib
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from agent import anthropic_credentials as ac
from agent.agent_runtime_helpers import _recover_auth_failure
from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential
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
    assert retry.anthropic_401_retry_attempted is True
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


# ── unified per-iteration budget: early refresh and late rotation share ONE guard ─────────


def test_early_refresh_success_consumes_budget_no_second_retry_in_settle(cred_file):
    """A 401 recovered by the early ``recover_after_classification`` refresh must NOT also
    be eligible for a second retry via the late rotation path in the SAME iteration."""
    from agent.turn_recovery import recover_after_classification

    cred_file(NEW)
    agent = _make_agent()
    agent._recover_with_credential_pool = MagicMock(return_value=(False, False))
    retry = TurnRetryState()
    err = _revoked_401()
    classified = classify_api_error(err, provider=agent.provider, model=agent.model)
    messages = _messages()
    api_messages = [{"role": "system", "content": "SYSTEM PROMPT (byte-stable)"}] + copy.deepcopy(messages)

    with patch.object(agent, "_try_refresh_anthropic_client_credentials", return_value=True):
        recovered, _ = recover_after_classification(
            agent, err, classified, retry, status_code=401, error_context={},
            messages=messages, api_messages=api_messages,
        )
    assert recovered is True
    assert retry.anthropic_401_retry_attempted is True

    # A repeated 401 in the same iteration must NOT get a second retry: the budget is spent.
    built, builder = _built_clients(agent)
    with builder, patch.object(ac, "refresh_anthropic_oauth_pure") as post:
        verdict, terminal, _, _ = _settle(agent, retry, messages, api_error=err)
    assert verdict.action == "return"
    terminal.assert_called_once()
    assert built == []                          # no second client rebuild
    post.assert_not_called()


def test_early_refresh_failure_leaves_budget_for_rotation_retry(cred_file):
    """When the early refresh does NOT recover the 401, the late rotation path still gets
    its one retry against the live credential source."""
    from agent.turn_recovery import recover_after_classification

    cred_file(OLD)  # file still holds the failed token — nothing to adopt yet
    agent = _make_agent()
    agent._recover_with_credential_pool = MagicMock(return_value=(False, False))
    retry = TurnRetryState()
    err = _revoked_401()
    classified = classify_api_error(err, provider=agent.provider, model=agent.model)
    messages = _messages()
    api_messages = [{"role": "system", "content": "SYSTEM PROMPT (byte-stable)"}] + copy.deepcopy(messages)

    with patch.object(agent, "_try_refresh_anthropic_client_credentials", return_value=False):
        recovered, _ = recover_after_classification(
            agent, err, classified, retry, status_code=401, error_context={},
            messages=messages, api_messages=api_messages,
        )
    assert recovered is False
    assert retry.anthropic_401_retry_attempted is False    # budget still available

    # A peer rotates the file between the failed refresh and the settle-phase re-check.
    cred_file(NEW)
    built, builder = _built_clients(agent)
    with builder, patch.object(ac, "refresh_anthropic_oauth_pure") as post:
        verdict, terminal, _, _ = _settle(agent, retry, messages, api_error=err)
    assert verdict.action == "continue"
    terminal.assert_not_called()
    assert built == [NEW]
    assert retry.anthropic_401_retry_attempted is True
    post.assert_not_called()


# Native Anthropic SDK wire and two-agent credential-generation invariants.

def test_swapped_anthropic_request_client_sends_rotated_bearer(monkeypatch, caplog):
    seen = []
    old, new = "sk-ant-oat01-old-probe", "sk-ant-oat01-new-probe"

    def endpoint(request):
        seen.append(request.headers.get("authorization"))
        status = 200 if seen[-1] == f"Bearer {new}" else 401
        if status == 401:
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "OAuth access token has been revoked."}})
        return httpx.Response(200, json={"id": "msg_probe", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})

    transport = httpx.MockTransport(endpoint)
    def build(token, base_url, **kwargs):
        return anthropic.Anthropic(auth_token=token, base_url="https://probe.invalid", http_client=httpx.Client(transport=transport), max_retries=0)

    agent = AIAgent.__new__(AIAgent)
    agent.provider = "anthropic"
    agent.api_mode = "anthropic_messages"
    agent.model = "claude-sonnet-4-5"
    agent.base_url = "https://probe.invalid"
    agent._anthropic_base_url = agent.base_url
    agent._anthropic_api_key = old
    agent._anthropic_client = build(old, agent.base_url)
    agent._is_anthropic_oauth = True
    agent._oauth_1m_beta_disabled = False
    monkeypatch.setattr(agent, "_build_direct_anthropic_client", build)
    monkeypatch.setattr(agent, "_build_anthropic_client_for_key", lambda key: build(key[1], key[2]))
    monkeypatch.setattr(agent, "_try_refresh_anthropic_client_credentials", lambda **kwargs: False)
    kwargs = {"model": agent.model, "max_tokens": 32, "messages": [{"role": "user", "content": "probe"}]}
    first = agent._create_request_anthropic_client(reason="probe")
    try:
        first.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        pass
    else:
        raise AssertionError("old bearer was not revoked")
    agent._close_request_anthropic_client(first, reason="stream_error_cleanup")
    entry = type("Entry", (), {"runtime_api_key": new, "runtime_base_url": agent.base_url, "id": "probe"})()
    agent._swap_credential(entry)
    agent._anthropic_retry_bearer_log_pending = True
    with caplog.at_level(logging.INFO, logger="run_agent"):
        retry = agent._create_request_anthropic_client(reason="probe-retry")
    assert f"sha256={hashlib.sha256(new.encode()).hexdigest()[:12]}" in caplog.text
    assert retry.messages.create(**kwargs).content[0].text == "ok"
    assert seen == [f"Bearer {old}", f"Bearer {new}"]
    agent._close_request_anthropic_client(retry, reason="request_complete")
    # The last-chance adoption path rebuilds only the shared client; its pool
    # identity must follow the same generation for a subsequent 401.
    agent._try_refresh_anthropic_client_credentials = AIAgent._try_refresh_anthropic_client_credentials.__get__(agent)
    latest = "sk-ant-oat01-latest-probe"
    assert agent._try_refresh_anthropic_client_credentials(token=latest)
    assert agent.api_key == latest


def test_concurrent_401_adopts_peer_pool_token_without_another_refresh():
    """A peer already rotated the shared grant; the stale request must not rotate it again."""
    old, new = "sk-ant-oat01-old-probe", "sk-ant-oat01-new-probe"
    peer = AIAgent.__new__(AIAgent)
    peer.provider = "anthropic"
    peer.api_mode = "anthropic_messages"
    peer._is_anthropic_oauth = True
    peer._anthropic_api_key = old
    peer._is_entitlement_failure = MagicMock(return_value=False)
    peer._swap_credential = MagicMock()
    entry = SimpleNamespace(id="shared", runtime_api_key=old, source="claude_code")
    pool = MagicMock()
    pool.entries.return_value = [entry]
    # First AIAgent minted a replacement from the (simulated) shared endpoint;
    # its refresh invalidates the old bearer. The second agent's 401 is stale.
    owner = AIAgent.__new__(AIAgent)
    owner.provider = "anthropic"
    owner.api_mode = "anthropic_messages"
    owner._is_anthropic_oauth = True
    owner._is_entitlement_failure = MagicMock(return_value=False)
    owner._swap_credential = MagicMock()
    def refresh(**_kwargs):
        entry.runtime_api_key = new
        return entry
    pool.try_refresh_matching.side_effect = refresh
    assert _recover_auth_failure(
        owner, pool, status_code=401, has_retried_429=False, error_context={},
        api_key_hint=old, credential_id="shared", rotate_and_swap=MagicMock(),
    )[0]
    pool.try_refresh_matching.assert_called_once()
    pool.try_refresh_matching.reset_mock()
    recovered, _ = _recover_auth_failure(
        peer, pool, status_code=401, has_retried_429=False, error_context={},
        api_key_hint=old, credential_id="shared", rotate_and_swap=MagicMock(),
    )
    assert recovered is True
    peer._swap_credential.assert_called_once_with(entry)
    pool.try_refresh_matching.assert_not_called()


def test_two_agents_converge_on_one_refresh_and_retry_on_new_wire_bearer(tmp_path, monkeypatch):
    """Real pools, singleton file and Anthropic SDK wire; only the OAuth HTTP endpoint is faked."""
    import hermes_cli.auth as auth_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth_mod, "_global_auth_file_path", lambda: None)
    path = tmp_path / "claude" / ".credentials.json"
    path.parent.mkdir()
    monkeypatch.setattr(ac, "claude_code_credentials_path", lambda: path)
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)
    old, new = "sk-ant-oat01-old-rotation", "sk-ant-oat01-new-rotation"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": old, "refreshToken": "refresh-old",
        "expiresAt": int(time.time() * 1000) + 3_600_000,
    }}))

    class FakeOAuthEndpoint:
        current = "sk-ant-oat01-revoked-before-probe"
        refresh_token = "refresh-old"
        posts = 0

        def refresh(self, refresh_token, *, use_json=False):
            assert refresh_token == self.refresh_token
            self.posts += 1
            self.current, self.refresh_token = new, "refresh-new"
            return {"access_token": new, "refresh_token": "refresh-new",
                    "expires_at_ms": int(time.time() * 1000) + 3_600_000}

    endpoint = FakeOAuthEndpoint()
    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", endpoint.refresh)
    seen = []

    def serve(request):
        bearer = request.headers.get("authorization")
        seen.append(bearer)
        if bearer != f"Bearer {endpoint.current}":
            return httpx.Response(401, json={"type": "error", "error": {
                "type": "authentication_error", "message": "OAuth access token has been revoked."}})
        return httpx.Response(200, json={"id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})

    transport = httpx.MockTransport(serve)
    def build(token, _base_url):
        return anthropic.Anthropic(auth_token=token, base_url="https://api.anthropic.com",
            max_retries=0, http_client=httpx.Client(transport=transport))

    entry = PooledCredential(provider="anthropic", id="shared", label="Claude Code",
        auth_type=AUTH_TYPE_OAUTH, priority=0, source="claude_code",
        access_token=old, refresh_token="refresh-old", expires_at_ms=int(time.time() * 1000) + 3_600_000)
    agents = []
    kwargs = {"model": "claude-sonnet-4-5", "max_tokens": 16,
              "messages": [{"role": "user", "content": "hello"}]}
    for _ in range(2):
        agent = AIAgent.__new__(AIAgent)
        agent.provider, agent.api_mode, agent.model = "anthropic", "anthropic_messages", "claude-sonnet-4-5"
        agent.base_url = agent._anthropic_base_url = "https://api.anthropic.com"
        agent.api_key = agent._anthropic_api_key = old
        agent._is_anthropic_oauth = True
        agent._credential_pool_entry_id = "shared"
        agent._is_entitlement_failure = MagicMock(return_value=False)
        agent._build_direct_anthropic_client = build
        agent._build_anthropic_client_for_key = lambda key: build(key[1], key[2])
        agent._anthropic_client = build(old, agent.base_url)
        pool = CredentialPool("anthropic", [entry])
        agents.append((agent, pool))

    for agent, _pool in agents:
        with pytest.raises(anthropic.AuthenticationError):
            agent._create_request_anthropic_client(reason="before-401").messages.create(**kwargs)
    for agent, pool in agents:
        assert _recover_auth_failure(agent, pool, status_code=401, has_retried_429=False,
            error_context={}, api_key_hint=old, credential_id="shared", rotate_and_swap=MagicMock())[0]
        assert agent._create_request_anthropic_client(reason="retry").messages.create(**kwargs).content[0].text == "ok"
    assert endpoint.posts == 1
    assert seen == [f"Bearer {old}", f"Bearer {old}", f"Bearer {new}", f"Bearer {new}"]
