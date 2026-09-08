"""delegate_task's description must describe the dispatch mode this session actually gets.

A one-shot Kanban worker cannot receive a detached completion
(`async_delivery_supported()` is False for it, #63169), so `_dispatch_background`
falls back to running the batch inline. The schema nevertheless told every caller
"dispatch returns immediately ... Do NOT wait or poll" — so a worker planned around a
handle it would never receive, and instead sat blocking for the child's whole runtime.

The dispatch-mode paragraph is resolved from the SESSION's own capability (per-session
ContextVar / worker env), not from a process env flag, so it is stable for the life of a
conversation — the prompt-caching invariant holds — while still differing correctly
between two sessions served by one process.
"""

from __future__ import annotations

import pytest

from gateway.session_context import declare_stateless_channel, reset_session_vars
from tools.delegate_tool import _build_dispatch_mode_paragraph, _build_top_level_description

_BACKGROUND_MARKER = "dispatch returns immediately"
_BLOCKING_MARKER = "BLOCKS until every child finishes"


@pytest.fixture(autouse=True)
def _clean_session(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    reset_session_vars()
    yield
    reset_session_vars()


def test_ordinary_session_keeps_the_background_guidance():
    desc = _build_top_level_description()
    assert _BACKGROUND_MARKER in desc
    assert _BLOCKING_MARKER not in desc


def test_kanban_worker_is_told_the_call_blocks(monkeypatch):
    """The session the bug was reported from: inline execution, so say so."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    desc = _build_top_level_description()
    assert _BLOCKING_MARKER in desc
    assert _BACKGROUND_MARKER not in desc
    assert "Do NOT wait or poll" not in desc


def test_stateless_channel_is_told_the_call_blocks():
    """Same contract via the other route into inline execution (stateless HTTP, cron)."""
    declare_stateless_channel()
    assert _BLOCKING_MARKER in _build_top_level_description()


def test_paragraph_is_stable_within_a_session(monkeypatch):
    """Byte-stable across rebuilds while the session's capability is unchanged: the
    description is rebuilt on every get_definitions() pass and must not churn the
    cached prompt prefix."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    assert _build_dispatch_mode_paragraph() == _build_dispatch_mode_paragraph()
    assert _build_top_level_description() == _build_top_level_description()


def test_capability_probe_failure_keeps_the_default(monkeypatch):
    """A broken probe must not tell an ordinary session its calls block."""
    def _boom():
        raise RuntimeError("session context unavailable")

    monkeypatch.setattr("gateway.session_context.async_delivery_supported", _boom)
    assert _BACKGROUND_MARKER in _build_dispatch_mode_paragraph()


def test_description_still_carries_the_shared_rules():
    """The refactor into head/mode/tail must not drop any of the surrounding guidance."""
    for marker in ("USE FOR:", "DO NOT USE FOR", "RULES:", "SELF-REPORTS", "inherit the parent model"):
        assert marker in _build_top_level_description()
