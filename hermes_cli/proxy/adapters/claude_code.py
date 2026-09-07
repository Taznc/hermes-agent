"""Claude Code subscription adapter for the local OpenAI-compatible proxy.

The Claude subscription is a native Anthropic Messages endpoint rather than an
OpenAI-compatible one.  This adapter owns only credential discovery/refresh;
wire conversion is deliberately kept in ``claude_translate`` so the generic
proxy server remains a safe pass-through for regular OpenAI-compatible routes.
"""
from __future__ import annotations

from typing import FrozenSet

from agent.anthropic_credentials import (
    claude_code_credentials_path,
    is_claude_code_token_valid,
    read_claude_code_credentials,
    _resolve_claude_code_token_from_credentials,
)
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

_ALLOWED_PATHS: FrozenSet[str] = frozenset({"/chat/completions", "/models"})


class ClaudeCodeAdapter(UpstreamAdapter):
    """Claude Code OAuth credentials, refreshed only through its shared safe helper."""

    auth_hint = "claude /login"

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def display_name(self) -> str:
        return "Claude Code subscription"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    @property
    def transforms_openai_chat(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        creds = read_claude_code_credentials()
        return bool(creds and (is_claude_code_token_valid(creds) or creds.get("refreshToken")))

    def get_credential(self) -> UpstreamCredential:
        creds = read_claude_code_credentials()
        if not creds:
            raise RuntimeError("No Claude Code credentials found. Run `claude /login` first.")
        token = _resolve_claude_code_token_from_credentials(creds)
        if not token:
            raise RuntimeError("Claude Code credentials could not be refreshed. Run `claude /login` again.")
        return UpstreamCredential(bearer=token, base_url="https://api.anthropic.com/v1")

    def credential_path(self):
        """The external CLI-owned credential location, exposed only for diagnostics/tests."""
        return claude_code_credentials_path()


__all__ = ["ClaudeCodeAdapter"]
