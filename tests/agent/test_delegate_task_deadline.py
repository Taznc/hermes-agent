"""delegate_task must not be abandoned by the generic tool-batch deadline.

The parent's blocking `delegate_task` call was bounded by `timeouts.tools.concurrent_batch`
(420 s default) while the child's own bound, `delegation.child_timeout_seconds`, is 0 (no
cap) by default. Children that ran past 420 s therefore surfaced to the parent as
`[error] timed out` with their result discarded, while the child ran on to completion —
twice in one Kanban worker run, ~14 minutes of work delivered to nobody.

There is no async rail to fall back to in that session: `async_delivery_supported()` is
False for one-shot Kanban workers (#63169), so a detached completion would have no consumer
at all. The fix is therefore option (b) from the card — the blocking wait honors the
CHILD's deadline.
"""

from __future__ import annotations

import threading
import time

import pytest

import agent.tool_executor as tool_executor
from agent.tool_executor import (
    _ManagedToolResult,
    _ToolTimeoutResult,
    _delegate_task_timeout,
    _run_sequential_tool_execution_middleware,
    _sequential_tool_deadline,
)


class _FakeAgent:
    def __init__(self):
        self._tool_worker_threads = set()
        self._tool_worker_threads_lock = threading.Lock()
        self._interrupt_requested = False

    def _touch_activity(self, msg):
        pass


@pytest.fixture
def fake_agent():
    return _FakeAgent()


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(tool_executor, "_SEQUENTIAL_INTERRUPT_POLL_SECONDS", 0.05)
    emitted = []
    monkeypatch.setattr(
        tool_executor, "_emit_terminal_post_tool_call", lambda agent, **kw: emitted.append(kw)
    )
    return emitted


@pytest.fixture
def tight_batch_deadline(monkeypatch):
    """A tool-batch deadline far shorter than the child, i.e. the shipped 420 s vs a
    multi-minute subagent, compressed so the test runs in under a second."""
    monkeypatch.setattr(tool_executor, "_resolve_concurrent_tool_timeout", lambda: 0.2)
    return monkeypatch


def _slow_child(seconds: float):
    def _fake_middleware(agent_arg, **kwargs):
        time.sleep(seconds)
        return _ManagedToolResult(
            result='{"results": [{"summary": "P1 finding"}]}', args={},
            middleware_trace=[], blocked=False, dispatched=True,
        )

    return _fake_middleware


def test_delegate_survives_tool_batch_deadline(monkeypatch, fake_agent, tight_batch_deadline):
    """A child running past the tool-batch deadline still returns its result to the parent."""
    monkeypatch.setattr(tool_executor, "_run_agent_tool_execution_middleware", _slow_child(0.6))
    monkeypatch.setattr(tool_executor, "_delegate_task_timeout", lambda: None)

    managed = _run_sequential_tool_execution_middleware(
        fake_agent,
        function_name="delegate_task",
        function_args={"tasks": [{"goal": "review"}]},
        effective_task_id="t",
        tool_call_id="call_1",
        execute=lambda a: "unused",
    )

    assert not isinstance(managed.result, _ToolTimeoutResult)
    assert "P1 finding" in managed.result


def test_other_tools_keep_the_batch_deadline(monkeypatch, fake_agent, tight_batch_deadline):
    """The exemption is delegate_task-only — nothing else gains an unbounded wait."""
    monkeypatch.setattr(tool_executor, "_run_agent_tool_execution_middleware", _slow_child(0.6))

    managed = _run_sequential_tool_execution_middleware(
        fake_agent,
        function_name="web_search",
        function_args={},
        effective_task_id="t",
        tool_call_id="call_2",
        execute=lambda a: "unused",
    )

    assert isinstance(managed.result, _ToolTimeoutResult)


def test_delegate_still_interruptible_without_a_deadline(monkeypatch, fake_agent, tight_batch_deadline):
    """Unbounded is not uninterruptible: /stop still abandons the wait within poll+grace."""
    started = threading.Event()

    def _fake_middleware(agent_arg, **kwargs):
        started.set()
        time.sleep(30)
        return _ManagedToolResult(
            result="late", args={}, middleware_trace=[], blocked=False, dispatched=True
        )

    monkeypatch.setattr(tool_executor, "_run_agent_tool_execution_middleware", _fake_middleware)
    monkeypatch.setattr(tool_executor, "_delegate_task_timeout", lambda: None)

    def _interrupt_soon():
        started.wait(5)
        time.sleep(0.1)
        fake_agent._interrupt_requested = True

    threading.Thread(target=_interrupt_soon, daemon=True).start()

    t0 = time.monotonic()
    managed = _run_sequential_tool_execution_middleware(
        fake_agent,
        function_name="delegate_task",
        function_args={"tasks": [{"goal": "review"}]},
        effective_task_id="t",
        tool_call_id="call_3",
        execute=lambda a: "unused",
    )

    assert "cancelled" in str(managed.result)
    assert time.monotonic() - t0 < 10.0


# ── deadline resolution ────────────────────────────────────────────────────


def test_deadline_defaults_to_unbounded_when_child_has_no_cap(monkeypatch):
    """delegation.child_timeout_seconds=0 (shipped default) means the parent must not
    impose one either — otherwise the parent gives up on a child that never will."""
    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", lambda: None)
    monkeypatch.setattr(tool_executor, "_resolve_concurrent_tool_timeout", lambda: 420.0)
    assert _delegate_task_timeout() is None


def test_deadline_derives_from_child_cap_plus_grace(monkeypatch):
    """With a configured child cap the parent waits slightly LONGER, so the child's own
    structured timeout entry (reason, api_calls, diagnostic path) is what surfaces."""
    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", lambda: 900.0)
    resolved = _delegate_task_timeout()
    assert resolved is not None and resolved > 900.0


def test_explicit_config_key_wins(monkeypatch):
    """timeouts.tools.delegate_task overrides both, for an operator who wants a hard cap."""
    def _resolve(key, *, default, env_var=None):
        return 55.0 if key == "tools.delegate_task" else default

    monkeypatch.setattr("agent.deadline.resolve_timeout", _resolve)
    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", lambda: None)
    assert _delegate_task_timeout() == 55.0


def test_child_timeout_lookup_failure_is_unbounded(monkeypatch):
    """A broken config read must not resurrect the 420 s abandonment."""
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", _boom)
    assert _delegate_task_timeout() is None


def test_deadline_routing_is_name_scoped(monkeypatch):
    """One dispatcher, two policies: only delegate_task takes the child-derived deadline."""
    monkeypatch.setattr(tool_executor, "_delegate_task_timeout", lambda: 1234.0)
    monkeypatch.setattr(tool_executor, "_resolve_sequential_tool_timeout", lambda: 42.0)

    assert _sequential_tool_deadline("delegate_task") == 1234.0
    for other in ("web_search", "terminal", "clarify", ""):
        assert _sequential_tool_deadline(other) == 42.0
