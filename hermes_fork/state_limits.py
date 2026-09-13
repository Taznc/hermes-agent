"""Fork session-state helpers: pure config parsing and SQL projection builders
extracted from ``hermes_state*.py`` behind ``# >>> FORK ANCHOR`` seams.

The public names stay on their upstream modules (``hermes_state`` /
``hermes_state_common``) so callers and tests keep the pre-extraction import
paths; only the fork-owned bodies live here.
"""

from __future__ import annotations

_RATE_LIMIT_RECOVERY_CHOICES = ("ask", "resume_at_reset")


def resolved_rate_limit_default_recovery() -> str:
    """Config-resolved ``sessions.rate_limit_default_recovery`` (Phase 2.12).

    Desktop-facing default action when a turn's wire error descriptor
    (``agent.error_surface``) carries a usable ``reset_at`` for a provider
    rate limit: "ask" (default) waits for the user to pick an action;
    "resume_at_reset" lets Desktop auto-schedule a resume once the failure
    card's countdown isn't cancelled. The backend itself never consults this
    value to auto-resume a session on its own — it exists purely so every
    Desktop-facing surface reads the same one config key instead of each
    inventing its own default. An unrecognized or missing value falls back
    to "ask" rather than silently enabling unattended auto-resume.
    """
    try:
        from hermes_cli.config import load_config_readonly

        sessions_cfg = load_config_readonly().get("sessions") or {}
        value = str(sessions_cfg.get("rate_limit_default_recovery") or "").strip()
        if value in _RATE_LIMIT_RECOVERY_CHOICES:
            return value
        return "ask"
    except Exception:
        return "ask"


def sql_served_route_column(alias: str, column: str) -> str:
    """SQL expression for the ACTUALLY-SERVED route on a session's most
    recent completed turn (Phase 2.13 sidebar identity).

    ``sessions.model`` / ``sessions.billing_provider`` are only ever the
    FIRST accounted route (``update_token_counts`` COALESCE-backfills them —
    see its docstring) — a silent mid-conversation fallback never rewrites
    them, so they go stale the moment a session falls back after its first
    successful call. ``session_model_usage`` (schema v20, per-model
    attribution) records every distinct (model, provider) route touched by
    the main loop (``task=''``) with a ``last_seen`` timestamp, so the
    freshest main-loop row there IS the latest-served route. Falls back to
    the ``sessions`` column for legacy sessions with no session_model_usage
    rows (pre-v20, or a session that never recorded a delta) — which also
    naturally reads as "no mismatch" against the configured route.

    ``column`` must be ``"model"`` or ``"billing_provider"``.
    """
    return (
        f"COALESCE(NULLIF((SELECT smu.{column} FROM session_model_usage smu "
        f"WHERE smu.session_id = {alias}.id AND smu.task = '' "
        f"ORDER BY smu.last_seen DESC LIMIT 1), ''), {alias}.{column})"
    )


def sql_served_route_columns(alias: str = "s") -> str:
    """``served_model`` + ``served_provider`` projection tail for a ``sessions {alias}`` row
    (comma-led so it appends to an existing SELECT list)."""
    return (
        f",\n                    {sql_served_route_column(alias, 'model')} AS served_model,\n"
        f"                    {sql_served_route_column(alias, 'billing_provider')} AS served_provider"
    )
