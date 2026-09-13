"""A delegated child's plain-text summary is its terminal state, not a protocol violation.

`delegate_task` children inherit the dispatcher worker's ``HERMES_KANBAN_*`` env, so the
kanban stop guard used to fire inside them: the child was told a plain-text reply "is NOT a
terminal state for the board" and pushed to call a board tool, which the delegated-child
mutation guard (correctly) refuses — so the review it had already finished was rewritten
into a "parent must record the verdict" apology and the parent got nothing.

These tests pin the gate verdict, which is what decides the child's ``exit_reason``:
``apply_stop_gates`` returning ``continue_turn=False`` with ``final_response`` intact is
exactly the path on which ``run_conversation`` reports ``completed=True``, and
``tools/delegate_tool_child_run.py`` maps that to ``exit_reason="completed"``.
"""

from __future__ import annotations

import pytest

import agent.turn_stop_gates as turn_stop_gates
from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, delegated_child_context
from agent.turn_stop_gates import apply_stop_gates

_SUMMARY = "P1: the retry loop swallows the 429 and reports success. Fix at client.py:212."


class _FakeAgent:
    """Minimum surface apply_stop_gates touches on the continue path."""

    def __init__(self):
        self.session_id = "child-session"
        self.platform = "cli"
        self.model = "test-model"
        self._turn_file_mutation_paths = set()
        self._session_messages = None
        self.statuses = []
        self.interim = []

    def _emit_interim_assistant_message(self, msg):
        self.interim.append(msg)

    def _flush_messages_to_session_db(self, messages, conversation_history=None):
        return True

    def _interim_content_was_streamed(self, text):
        return False

    def _emit_status(self, text):
        self.statuses.append(text)


@pytest.fixture
def agent():
    return _FakeAgent()


@pytest.fixture(autouse=True)
def _only_the_kanban_gate(monkeypatch):
    """Isolate the kanban gate: the verify-on-stop and pre_verify gates are unrelated here."""
    monkeypatch.setattr(turn_stop_gates, "_verify_on_stop_nudge", lambda _agent: None)
    monkeypatch.setattr(turn_stop_gates, "_pre_verify_nudge", lambda _agent, _resp, _n: None)


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    return monkeypatch


def _run(agent, messages):
    return apply_stop_gates(
        agent,
        {"role": "assistant", "content": _SUMMARY},
        final_response=_SUMMARY,
        messages=messages,
        conversation_history=None,
        pending_verification_response=None,
        pending_verification_response_previewed=False,
    )


def test_delegated_child_plain_text_is_terminal(agent, worker_env):
    """In child context the gate passes the summary through untouched."""
    messages = [{"role": "user", "content": "review the retry loop"}]

    with delegated_child_context("child-session"):
        verdict = _run(agent, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == _SUMMARY
    # No synthetic nudge row, so the child's own answer is the last word.
    assert messages == [{"role": "user", "content": "review the retry loop"}]
    assert agent.statuses == []


def test_dispatcher_worker_still_nudged(agent, worker_env):
    """The guard the child fix must not disarm: a real worker's narrated stop still continues."""
    messages = [{"role": "user", "content": "work kanban task"}]

    verdict = _run(agent, messages)

    assert verdict.continue_turn is True
    assert verdict.final_response is None
    assert verdict.pending_verification_response == _SUMMARY
    nudge = messages[-1]
    assert nudge["role"] == "user" and nudge["_kanban_stop_synthetic"] is True
    assert "kanban_complete" in nudge["content"]
    assert agent.statuses  # the worker-facing warning is emitted


def test_delegated_child_subprocess_plain_text_is_terminal(agent, worker_env):
    """Lineage crossing a fork (env marker, no ContextVar) is covered by the same gate."""
    worker_env.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
    messages = [{"role": "user", "content": "review the retry loop"}]

    verdict = _run(agent, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == _SUMMARY
    assert len(messages) == 1
