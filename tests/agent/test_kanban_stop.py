"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    delegated_child_context,
    non_dispatcher_owned_context,
)
from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


_TERMINAL_VERBS = (
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE", DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ── Ownership: HERMES_KANBAN_TASK is inherited, not proof of ownership ──────
# A delegate_task child, a subprocess it spawns, and an in-process cron job all
# see the dispatcher worker's task id while owning no board run. For them a
# plain-text answer IS the terminal state; nudging one makes it chase board
# tools the mutation guard (correctly) refuses and rewrite its finished work
# into an apology.


def test_no_nudge_in_delegated_child_context(clear_kanban_env):
    """The in-process delegate_task child (ContextVar) is not a board worker."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_x")
    assert kanban_stop_nudge_enabled() is True  # the worker itself still gets it

    with delegated_child_context("child-session"):
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[], attempts=0) is None

    # Ownership is restored when the child context exits.
    assert kanban_stop_nudge_enabled() is True


def test_no_nudge_in_delegated_child_subprocess(clear_kanban_env):
    """Lineage crosses fork via the env marker, so a child's subprocess is covered too."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_x")
    clear_kanban_env.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[], attempts=0) is None


def test_no_nudge_for_in_process_cron_job(clear_kanban_env):
    """A cron job fired inside a worker inherits the env but owns no run."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_x")
    with non_dispatcher_owned_context():
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[], attempts=0) is None


def test_ownership_probe_fails_open(clear_kanban_env):
    """A raising delegation-context probe must not silently disarm the guard for real workers."""
    import agent.delegation_context as delegation_context
    from agent.kanban_stop import _is_dispatcher_owned_worker

    def _boom():
        raise RuntimeError("delegation context unavailable")

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_x")
    clear_kanban_env.setattr(delegation_context, "is_dispatcher_owned_worker_context", _boom)
    assert _is_dispatcher_owned_worker() is True
    assert kanban_stop_nudge_enabled() is True




def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize("verb", _TERMINAL_VERBS)
def test_terminal_true_for_assistant_tool_calls_dict_shape(verb):
    """Each of the four board-terminal verbs suppresses the guard (dict tool-call shape)."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": verb, "arguments": "{}"}}
            ],
        },
    ]
    assert session_called_kanban_terminal(messages) is True


@pytest.mark.parametrize("verb", _TERMINAL_VERBS)
def test_terminal_true_for_assistant_tool_calls_object_shape(verb):
    """Each of the four board-terminal verbs suppresses the guard (object tool-call shape)."""
    tool_call = SimpleNamespace(function=SimpleNamespace(name=verb))
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
    ]
    assert session_called_kanban_terminal(messages) is True


@pytest.mark.parametrize("verb", _TERMINAL_VERBS)
def test_terminal_true_for_tool_role_message(verb):
    """A ``role: tool`` message named with each verb also suppresses the guard."""
    messages = [
        {"role": "tool", "name": verb, "tool_call_id": "1", "content": "ok"},
    ]
    assert session_called_kanban_terminal(messages) is True


def test_terminal_false_for_non_terminal_verb():
    """A worker that only commented (not a board-terminal action) still gets nudged."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "kanban_comment", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": "kanban_comment", "tool_call_id": "1", "content": "noted"},
    ]
    assert session_called_kanban_terminal(messages) is False


@pytest.mark.parametrize("verb", ["kanban_request_review", "kanban_request_changes"])
def test_no_nudge_after_review_lane_handoff(clear_kanban_env, verb):
    """build_kanban_stop_nudge returns None after a correct review-lane handoff."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": verb, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": verb, "tool_call_id": "1", "content": "ok"},
    ]
    assert build_kanban_stop_nudge(messages=messages) is None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the worker/CLI boundary parks it immediately: one
# no-evidence recovery is allowed, while handoff evidence or the next clean
# exit is blocked. See tests/hermes_cli/test_kanban_core_functionality.py for
# the boundary and dispatcher-side streak tests.




