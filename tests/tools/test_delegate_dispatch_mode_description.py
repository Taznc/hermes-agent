"""delegate_task's description must describe the dispatch mode this session actually gets.

A one-shot Kanban worker cannot receive a detached completion
(`async_delivery_supported()` is False for it, #63169) and has no session id to wake, so
`_dispatch_background` falls back to running the batch inline. The schema nevertheless told
every caller "dispatch returns immediately ... Do NOT wait or poll" — so a worker planned
around a handle it would never receive, and instead sat blocking for the child's whole runtime.

Two things this file pins:

* the paragraph comes from the SAME decision the dispatcher makes
  (`delegate_tool_dispatch.effective_dispatch_mode`, built on `_resolve_async_wake_sid`), so
  the schema cannot disagree with the runtime — notably on a wakeable API-server session,
  where async delivery is unsupported but a bound raw session id still dispatches in the
  background;
* the mode is part of `model_tools._tool_defs_cache_key`, so one session's memoized schema is
  never served to a session in the other mode. It stays session-scoped (ContextVars / the
  session's own env), so the description is byte-stable for the life of a conversation and the
  prompt-caching invariant holds.
"""

from __future__ import annotations

import pytest

from gateway.session_context import declare_stateless_channel, reset_session_vars, set_session_vars
from tools.delegate_tool import _build_dispatch_mode_paragraph, _build_top_level_description

_BACKGROUND_MARKER = "dispatch returns immediately"
_BLOCKING_MARKER = "BLOCKS until every child finishes"


@pytest.fixture(autouse=True)
def _clean_session(monkeypatch):
    from model_tools import _clear_tool_defs_cache

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    reset_session_vars()
    _clear_tool_defs_cache()
    yield
    reset_session_vars()
    _clear_tool_defs_cache()


def _schema_mode() -> str:
    """The dispatch mode delegate_task's description advertises, read through the REAL
    memoized schema path the agent uses (not the private paragraph builder)."""
    from model_tools import _tool_defs_cache_key, get_tool_definitions

    # A bypassed memo key would make every cache-leak assertion below vacuous.
    assert _tool_defs_cache_key(None, None, False) is not None, "definition caching is bypassed"
    for tool in get_tool_definitions(quiet_mode=True):
        if tool["function"]["name"] == "delegate_task":
            description = tool["function"]["description"]
            if _BACKGROUND_MARKER in description:
                return "background"
            if _BLOCKING_MARKER in description:
                return "blocking"
            return "unknown"
    raise AssertionError("delegate_task missing from the tool definitions")


def _bind_wakeable_api_session() -> None:
    """An api_server request: async delivery unsupported, but a raw session id IS bound, so
    `_resolve_async_wake_sid` dispatches in the background and wakes it by self-post."""
    set_session_vars(
        platform="api_server", chat_id="raw-sid-7", session_key="raw-sid-7",
        session_id="raw-sid-7", async_delivery=False,
    )


def test_ordinary_session_keeps_the_background_guidance():
    desc = _build_top_level_description()
    assert _BACKGROUND_MARKER in desc
    assert _BLOCKING_MARKER not in desc


def test_kanban_worker_is_told_the_call_blocks(monkeypatch):
    """The session the bug was reported from: inline execution, so say so. HERMES_KANBAN_TASK
    alone is the reported shape — async_delivery_supported() reads it directly."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    desc = _build_top_level_description()
    assert _BLOCKING_MARKER in desc
    assert _BACKGROUND_MARKER not in desc
    assert "Do NOT wait or poll" not in desc


def test_stateless_channel_is_told_the_call_blocks():
    """Same contract via the other route into inline execution (stateless HTTP, cron)."""
    declare_stateless_channel()
    assert _BLOCKING_MARKER in _build_top_level_description()


def test_delegated_child_is_told_the_call_blocks():
    """A child's own delegations are forced synchronous (_model_background_value at depth > 0),
    regardless of what the surrounding session could receive."""
    from agent.delegation_context import delegated_child_context

    with delegated_child_context():
        assert _BLOCKING_MARKER in _build_top_level_description()


def test_wakeable_api_session_is_told_it_dispatches_in_the_background():
    """async_delivery_supported() is False here, but the dispatcher still goes background
    because a raw session id is bound (test_delegate_apiserver_background.py pins that
    behavior). The description must not claim the call blocks."""
    _bind_wakeable_api_session()
    assert _BACKGROUND_MARKER in _build_top_level_description()
    assert _BLOCKING_MARKER not in _build_top_level_description()
    assert _schema_mode() == "background"


def test_session_without_a_wake_target_is_told_the_call_blocks():
    """The other half of the same predicate: no session id to wake => genuinely inline."""
    set_session_vars(platform="api_server", chat_id="", session_key="", session_id="", async_delivery=False)
    assert _BLOCKING_MARKER in _build_top_level_description()


def test_description_matches_the_dispatchers_own_decision():
    """One canonical decision drives both, so they cannot drift apart."""
    from tools.delegate_tool_dispatch import DISPATCH_MODE_BACKGROUND, DISPATCH_MODE_BLOCKING, effective_dispatch_mode

    for bind in (lambda: None, declare_stateless_channel, _bind_wakeable_api_session):
        reset_session_vars()
        bind()
        expected = _BLOCKING_MARKER if effective_dispatch_mode() == DISPATCH_MODE_BLOCKING else _BACKGROUND_MARKER
        assert effective_dispatch_mode() in (DISPATCH_MODE_BACKGROUND, DISPATCH_MODE_BLOCKING)
        assert expected in _build_dispatch_mode_paragraph()


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


# ---------------------------------------------------------------------------
# Cache safety: the memoized schema must not cross session boundaries.
# One process serves many sessions (gateway, API server, desktop backend); the
# dispatch mode is a property of the SESSION, so it belongs in the memo key.
# ---------------------------------------------------------------------------


def _cache_key():
    from model_tools import _tool_defs_cache_key

    return _tool_defs_cache_key(None, None, False)


def test_dispatch_mode_is_part_of_the_definition_cache_key():
    """The load-bearing assertion, independent of eviction and of the other key
    components: two sessions in different dispatch modes must not share a memo entry.
    The end-to-end tests below can pass incidentally when an unrelated component of the
    key (config mtime) happens to change between calls; this one cannot."""
    reset_session_vars()
    background_key = _cache_key()
    assert background_key == _cache_key(), "the key must be stable within one session"

    declare_stateless_channel()
    assert _cache_key() != background_key

    reset_session_vars()
    _bind_wakeable_api_session()
    assert _cache_key() == background_key, "a wakeable API session dispatches in the background too"


def test_blocking_session_does_not_inherit_a_cached_background_schema():
    """Cache order A: ordinary session first, then a blocking one in the same process."""
    assert _schema_mode() == "background"
    declare_stateless_channel()
    assert _schema_mode() == "blocking"


def test_background_session_does_not_inherit_a_cached_blocking_schema():
    """Cache order B: the reverse, which is the order that stranded a real worker's guidance."""
    declare_stateless_channel()
    assert _schema_mode() == "blocking"
    reset_session_vars()
    assert _schema_mode() == "background"


def test_kanban_worker_schema_does_not_leak_into_an_ordinary_session(monkeypatch):
    """The reported shape end to end: worker sees 'blocks', the next session does not."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    assert _schema_mode() == "blocking"
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    reset_session_vars()
    assert _schema_mode() == "background"

