import copy
from unittest.mock import MagicMock, patch

from agent import anthropic_credentials as ac
from agent.error_classifier import classify_api_error
from agent.turn_recovery import recover_after_classification
from agent.turn_retry_state import TurnRetryState
from tests.agent.test_anthropic_401_rotation_retry import (
    NEW, _built_clients, _make_agent, _messages, _revoked_401, _settle,
)


def test_early_refresh_then_rotation_path_shares_one_retry_budget(cred_file, monkeypatch):
    """A single API-call iteration must permit exactly ONE Anthropic credential retry,
    even though it can be granted by either the early refresh branch or the late rotation
    branch. Reproduces the double-retry regression from independent review (t_416a36af,
    comment 1315): the two branches used to guard themselves with independent one-shot
    flags, so a repeated 401 in the same iteration reached the rotation branch a second time.
    """
    # Recreate the helper fixture locally because this file is outside tests/agent.
    path = cred_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"claudeAiOauth":{"accessToken":"' + NEW + '","refreshToken":"rt-new","expiresAt":9999999999999}}')
    monkeypatch.setattr(ac, "claude_code_credentials_path", lambda: path)
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)

    agent = _make_agent()
    agent._recover_with_credential_pool = MagicMock(return_value=(False, False))
    retry = TurnRetryState()
    err = _revoked_401()
    classified = classify_api_error(err, provider=agent.provider, model=agent.model)
    messages = _messages()
    api_messages = [{"role": "system", "content": "SYSTEM PROMPT (byte-stable)"}] + copy.deepcopy(messages)

    # First 401 takes the pre-existing Anthropic credential refresh branch and consumes
    # the single per-iteration retry budget.
    with patch.object(agent, "_try_refresh_anthropic_client_credentials", return_value=True):
        recovered, _ = recover_after_classification(
            agent, err, classified, retry, status_code=401, error_context={},
            messages=messages, api_messages=api_messages,
        )
    assert recovered is True
    assert retry.anthropic_401_retry_attempted is True

    # A repeated 401 in the same iteration must NOT be granted a second retry via the
    # rotation branch: the budget is already spent, so the turn goes fatal.
    built, builder = _built_clients(agent)
    with builder, patch.object(ac, "refresh_anthropic_oauth_pure") as post:
        verdict, terminal, _, _ = _settle(agent, retry, messages, api_error=err)
    assert verdict.action == "return"
    terminal.assert_called_once()
    assert built == []
    post.assert_not_called()


import pytest
@pytest.fixture
def cred_file(tmp_path):
    return tmp_path / ".claude" / ".credentials.json"
