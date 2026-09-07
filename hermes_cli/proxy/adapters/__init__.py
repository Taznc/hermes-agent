"""Upstream adapter registry for the local proxy server (see :class:`UpstreamAdapter`)."""

from typing import Dict, Type

from hermes_cli.proxy.adapters.base import UpstreamAdapter
from hermes_cli.proxy.adapters.claude_code import ClaudeCodeAdapter
from hermes_cli.proxy.adapters.codex import OpenAICodexAdapter
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter
from hermes_cli.proxy.adapters.xai import XAIGrokAdapter

# Keyed by the ``hermes proxy start --provider <name>`` value.
ADAPTERS: Dict[str, Type[UpstreamAdapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "openai-codex": OpenAICodexAdapter,
    "nous": NousPortalAdapter,
    "xai": XAIGrokAdapter,
}
_ALIASES = {"codex": "openai-codex"}


def get_adapter(name: str) -> UpstreamAdapter:
    """Instantiate an adapter by name."""
    key = _ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    if key not in ADAPTERS:
        available = ", ".join(sorted(ADAPTERS)) or "(none)"
        raise ValueError(f"Unknown proxy upstream provider: {name!r}. Available: {available}")
    return ADAPTERS[key]()


__all__ = ["UpstreamAdapter", "ADAPTERS", "get_adapter"]
