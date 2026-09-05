"""Fork Desktop-safe account-limits view: fail-open per-provider snapshots,
specific unavailable reasons, and the sanitized JSON serialization shared by
the dashboard route and the gateway RPC method.

Extracted from ``agent/account_usage.py`` (see docs/fork-anchor-extraction.md
in the workspace repo). Only fork surfaces (``/api/account-limits``,
``account_limits.get``) call these; upstream code does not.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent import account_usage
from agent.account_usage import AccountUsageSnapshot, _utc_now
from hermes_cli.auth import AuthError, is_rate_limited_auth_error, resolve_codex_runtime_credentials

logger = logging.getLogger("agent.account_usage")

_ACCOUNT_LIMITS_PROVIDERS = ("anthropic", "openai-codex")
_NO_LIMITS_CREDENTIALS = "No compatible account credentials are configured for provider limits."


def serialize_account_usage(snapshot: AccountUsageSnapshot) -> dict[str, Any]:
    """Return a JSON-safe, explicitly allowlisted account-limits payload (Desktop/plugin surface)."""
    return {
        "provider": snapshot.provider, "source": snapshot.source, "fetched_at": snapshot.fetched_at.isoformat(),
        "title": snapshot.title, "plan": snapshot.plan, "available": snapshot.available,
        "allowed": snapshot.allowed, "limit_reached": snapshot.limit_reached,
        "unavailable_reason": snapshot.unavailable_reason, "details": list(snapshot.details),
        "windows": [
            {"label": w.label, "used_percent": w.used_percent,
             "reset_at": w.reset_at.isoformat() if w.reset_at else None, "detail": w.detail,
             "severity": w.severity, "is_active": w.is_active, "scope": w.scope,
             "limit_window_seconds": w.limit_window_seconds, "limit_reached": w.limit_reached}
            for w in snapshot.windows],
        "balances": [
            {k: v for k, v in (("label", b.label), ("used", b.used), ("limit", b.limit),
                               ("remaining", b.remaining), ("currency", b.currency)) if v is not None}
            for b in snapshot.balances],
    }


def _account_limits_unavailable_reason(provider: str) -> str:
    """Explain why a provider's limits are missing, in the user's terms. ``fetch_account_usage``
    deliberately fails open (``None`` for every failure mode), which made a rate-limited or expired
    account look identical to one never configured; re-derive the specific cause for the UI."""
    if provider != "openai-codex":
        return _NO_LIMITS_CREDENTIALS
    try:
        resolve_codex_runtime_credentials(refresh_if_expiring=True)
    except AuthError as exc:
        if is_rate_limited_auth_error(exc):
            # Quota is spent AND no usable token remained to read usage with (commonly an expired
            # stored token) — say so rather than implying the account is missing.
            return f"{exc} Sign in again with `hermes auth` to restore the usage view."
        if getattr(exc, "relogin_required", False):
            return "Codex sign-in has expired. Run `hermes auth` to reconnect the account."
    except Exception:  # pragma: no cover - diagnostics must never raise
        logger.debug("codex ▸ unavailable-reason probe failed", exc_info=True)
    return _NO_LIMITS_CREDENTIALS


def fetch_account_limits(providers: Optional[tuple[str, ...]] = None) -> tuple[AccountUsageSnapshot, ...]:
    """Desktop-safe account-limit view for supported providers: wraps the CLI's fail-open fetcher so a
    provider outage or missing credential becomes a per-provider unavailable snapshot, never an
    exception that breaks another provider's display."""
    normalized = tuple(str(p or "").strip().lower() for p in (providers or _ACCOUNT_LIMITS_PROVIDERS))
    invalid = tuple(p for p in normalized if p not in _ACCOUNT_LIMITS_PROVIDERS)
    if invalid:
        raise ValueError(f"Unsupported account-limit provider: {invalid[0]}")
    return tuple(
        account_usage.fetch_account_usage(p) or AccountUsageSnapshot(
            provider=p, source="account_limits", fetched_at=_utc_now(),
            unavailable_reason=_account_limits_unavailable_reason(p))
        for p in normalized)
