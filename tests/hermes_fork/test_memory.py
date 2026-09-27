"""``memory.external_prefetch_timeout`` (``hermes_fork.memory``) through the real agent path.

The ``memory-external-prefetch-timeout`` FORK ANCHOR in ``agent_init._init_memory``
is what is under test: each agent is built by the real ``AIAgent`` constructor
against a temp HERMES_HOME whose ``config.yaml`` selects a user-installed provider
from ``$HERMES_HOME/plugins/``. Config loading and provider discovery are not
patched; only the model client and tool catalog are.
"""

from __future__ import annotations

import contextlib
import logging
import math
from pathlib import Path
from unittest.mock import patch

import pytest

from agent import memory_manager
from agent.turn_context import _memory_turn_start_and_prefetch
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_fork import memory as fork_memory

SLOW_SECONDS = memory_manager._EXTERNAL_PREFETCH_TIMEOUT_S + 0.75
MARKER = "slowmem-recalled-fact"

_PROVIDER = f'''
import time
from agent.memory_provider import MemoryProvider

class SlowMem(MemoryProvider):
    name = "slowmem"
    def is_available(self):
        return True
    def initialize(self, session_id, **kw):
        pass
    def get_tool_schemas(self):
        return []
    def prefetch(self, query, *, session_id=""):
        time.sleep({SLOW_SECONDS!r})
        return "{MARKER}"

def register(ctx):
    ctx.register_memory_provider(SlowMem())
'''


def _home(root: Path, name: str, memory_yaml: str) -> Path:
    home = root / name
    provider_dir = home / "plugins" / "slowmem"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(_PROVIDER, encoding="utf-8")
    (home / "config.yaml").write_text(f"memory:\n  provider: slowmem\n{memory_yaml}", encoding="utf-8")
    return home


@contextlib.contextmanager
def _agent_in(home: Path, monkeypatch: pytest.MonkeyPatch):
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    try:
        with patch("model_tools.get_tool_definitions", return_value=[]), patch(
            "model_tools.check_toolset_requirements", return_value={}
        ), patch("agent.process_bootstrap.OpenAI"):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                platform="telegram",
                session_id=f"s-{home.name}",
            )
        try:
            yield agent
        finally:
            agent.close()
    finally:
        reset_hermes_home_override(token)


def test_provider_slower_than_default_cap_is_kept_when_key_is_30(tmp_path, monkeypatch):
    home = _home(tmp_path, "wide", "  external_prefetch_timeout: 30\n")
    with _agent_in(home, monkeypatch) as agent:
        assert agent._memory_manager is not None
        assert [p.name for p in agent._memory_manager.providers] == ["slowmem"]
        injected = _memory_turn_start_and_prefetch(agent, "what did we decide about the deploy?")
    assert MARKER in injected


def test_unset_key_keeps_upstream_default_and_drops_the_slow_provider(tmp_path, monkeypatch, caplog):
    home = _home(tmp_path, "default", "")
    with _agent_in(home, monkeypatch) as agent, caplog.at_level(logging.WARNING):
        assert agent._memory_manager._external_prefetch_timeout == memory_manager._EXTERNAL_PREFETCH_TIMEOUT_S
        injected = _memory_turn_start_and_prefetch(agent, "what did we decide about the deploy?")
    assert injected == ""
    assert "'slowmem' prefetch timed out" in caplog.text


def test_timeout_follows_the_profile_home_a_b_a(tmp_path, monkeypatch):
    a = _home(tmp_path, "a", "  external_prefetch_timeout: 30\n")
    b = _home(tmp_path, "b", "")
    observed = []
    for home in (a, b, a):
        with _agent_in(home, monkeypatch) as agent:
            observed.append(agent._memory_manager._external_prefetch_timeout)
    assert observed == [30.0, memory_manager._EXTERNAL_PREFETCH_TIMEOUT_S, 30.0]


def test_oversized_integer_warns_and_keeps_the_provider_at_the_default_cap(tmp_path, monkeypatch, caplog):
    # float(10**400) raises OverflowError; it must not escape into agent_init's
    # provider-init except, which would disable external memory entirely.
    monkeypatch.setattr(fork_memory, "_warned", set())
    home = _home(tmp_path, "huge", f"  external_prefetch_timeout: {10**400}\n")
    with caplog.at_level(logging.WARNING), _agent_in(home, monkeypatch) as agent:
        assert agent._memory_manager is not None
        assert [p.name for p in agent._memory_manager.providers] == ["slowmem"]
        assert agent._memory_manager._external_prefetch_timeout == memory_manager._EXTERNAL_PREFETCH_TIMEOUT_S
    assert caplog.text.count("Ignoring memory.external_prefetch_timeout") == 1


@pytest.mark.parametrize("raw, expected", [(30, 30.0), (12.5, 12.5), ("45", 45.0)])
def test_valid_values(raw, expected):
    assert fork_memory.external_prefetch_timeout({"external_prefetch_timeout": raw}) == expected


@pytest.mark.parametrize("mem_config", [None, {}, {"external_prefetch_timeout": None}, "not-a-dict"])
def test_unset_means_upstream_default(mem_config, caplog):
    with caplog.at_level(logging.WARNING):
        assert fork_memory.external_prefetch_timeout(mem_config) is None
    assert caplog.text == ""


@pytest.mark.parametrize("raw", [0, -1, "abc", True, math.nan, math.inf, [30], 10**400, "1" * 400])
def test_invalid_values_warn_and_fall_back(raw, caplog, monkeypatch):
    monkeypatch.setattr(fork_memory, "_warned", set())
    with caplog.at_level(logging.WARNING):
        assert fork_memory.external_prefetch_timeout({"external_prefetch_timeout": raw}) is None
        assert fork_memory.external_prefetch_timeout({"external_prefetch_timeout": raw}) is None
    assert caplog.text.count("Ignoring memory.external_prefetch_timeout") == 1
