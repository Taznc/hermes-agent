"""propose_new_session tool — the desktop inline new-session approval card's tool half.

Behavior contracts:
- no callback (not the desktop app) → tool_error pointing at continuing in this session
- empty/missing topic → tool_error
- callback answer passes through as JSON
- empty callback answer (timeout) → status "unanswered", never an error
"""

import json

import pytest

from tools.propose_new_session_tool import PROPOSE_NEW_SESSION_SCHEMA, propose_new_session_tool


def test_schema_mentions_new_session_and_topic():
    assert "new" in PROPOSE_NEW_SESSION_SCHEMA["description"].lower()
    assert "topic" in PROPOSE_NEW_SESSION_SCHEMA["parameters"]["properties"]


def test_requires_desktop_callback():
    result = json.loads(propose_new_session_tool(topic="Investigate the flaky test", callback=None))
    assert "error" in result
    assert "/new" in result["error"]


def test_requires_topic():
    result = json.loads(propose_new_session_tool(topic="  ", callback=lambda *a: ""))
    assert "error" in result


def test_passes_through_renderer_outcome():
    outcome = {"status": "approved", "session_id": "abc123"}

    def cb(topic, reason):
        assert topic == "Investigate the flaky test"
        assert reason == "unrelated to the current refactor"
        return json.dumps(outcome)

    result = json.loads(
        propose_new_session_tool(
            topic="Investigate the flaky test", reason="unrelated to the current refactor", callback=cb)
    )
    assert result == outcome


def test_timeout_returns_unanswered_not_error():
    result = json.loads(propose_new_session_tool(topic="New topic", callback=lambda *a: ""))
    assert result["status"] == "unanswered"


def test_callback_exception_is_tool_error():
    def cb(*a):
        raise RuntimeError("gateway went away")

    result = json.loads(propose_new_session_tool(topic="New topic", callback=cb))
    assert "error" in result


def test_non_json_answer_wrapped_as_error_status():
    result = json.loads(propose_new_session_tool(topic="New topic", callback=lambda *a: "garbage"))
    assert result["status"] == "error"


@pytest.mark.parametrize("status", ["approved", "declined"])
def test_approve_and_decline_pass_through(status):
    result = json.loads(
        propose_new_session_tool(
            topic="New topic", callback=lambda t, r: json.dumps({"status": status}))
    )
    assert result["status"] == status
