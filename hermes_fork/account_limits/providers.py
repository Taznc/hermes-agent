"""Fork payload→snapshot mapping for provider usage endpoints.

Extracted from ``agent/account_usage.py`` behind ``# >>> FORK ANCHOR`` call
sites (see docs/fork-anchor-extraction.md in the workspace repo). Credential
resolution, HTTP fetching, and the snapshot dataclasses stay upstream; the
fork's expanded parsing/normalization (window metadata, code-review quotas,
self-describing Anthropic limits, balances) lives here.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.account_usage import (
    AccountUsageBalance,
    AccountUsageSnapshot,
    AccountUsageWindow,
    _codex_banked_resets,
    _is_num,
    _parse_dt,
    _plural,
    _snapshot,
    _title_case_slug,
)


def _opt_int(value: Any) -> Optional[int]:
    return int(value) if _is_num(value) else None


def _opt_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _usage_windows(
    source: dict, mapping: tuple[tuple[str, str], ...], used_key: str, reset_key: str, *, fraction: bool = False,
    limit_reached: Optional[bool] = None,
) -> list[AccountUsageWindow]:
    """Fork superset of upstream's ``_usage_windows``: also carries per-window
    ``limit_window_seconds``/``limit_reached`` metadata; ``limit_reached`` is a snapshot-wide
    override OR'd into each window (Codex ``rate_limit.limit_reached``)."""
    windows: list[AccountUsageWindow] = []
    for key, label in mapping:
        window = source.get(key) or {}
        used = window.get(used_key)
        if used is None:
            continue
        used = float(used)
        if fraction and used <= 1:
            used *= 100
        window_reached = _opt_bool(window.get("limit_reached"))
        windows.append(AccountUsageWindow(
            label=label, used_percent=used, reset_at=_parse_dt(window.get(reset_key)),
            limit_window_seconds=_opt_int(window.get("limit_window_seconds")),
            limit_reached=(bool(window_reached) or limit_reached) if (window_reached is not None or limit_reached is not None) else None))
    return windows


def codex_usage_snapshot(payload: dict) -> AccountUsageSnapshot:
    """Map a Codex ``/usage`` payload, keeping blocked-state metadata (``allowed`` /
    ``limit_reached`` / ``limit_window_seconds``), code-review quota windows (flat or
    nested), and a sanitized credits balance."""
    rate_limit = payload.get("rate_limit") or {}
    limit_reached = bool(rate_limit.get("limit_reached")) or rate_limit.get("allowed") is False
    windows = _usage_windows(rate_limit, (("primary_window", "Session"), ("secondary_window", "Weekly")),
                             "used_percent", "reset_at", limit_reached=limit_reached)
    # Code review quota: either a flat window or nested primary/secondary windows.
    code_review = payload.get("code_review_rate_limit") or {}
    cr_reached = _opt_bool(code_review.get("limit_reached"))
    if _is_num(code_review.get("used_percent")):
        windows.append(AccountUsageWindow(
            label="Code review", used_percent=float(code_review["used_percent"]),
            reset_at=_parse_dt(code_review.get("reset_at")),
            limit_window_seconds=_opt_int(code_review.get("limit_window_seconds")), limit_reached=cr_reached))
    else:
        for w in _usage_windows(code_review, (("primary_window", "Code review session"),
                                              ("secondary_window", "Code review week")), "used_percent", "reset_at"):
            windows.append(AccountUsageWindow(label=w.label, used_percent=w.used_percent, reset_at=w.reset_at,
                                              limit_window_seconds=w.limit_window_seconds, limit_reached=cr_reached))
    details: list[str] = []
    balances: list[AccountUsageBalance] = []
    count = _codex_banked_resets(payload)
    if count > 0:
        details.append(f"You have {count} reset{_plural(count)} banked - use /usage reset to activate")
    credits, balance = payload.get("credits") or {}, (payload.get("credits") or {}).get("balance")
    if credits.get("has_credits") and _is_num(balance):
        details.append(f"Credits balance: ${float(balance):.2f}")
        balances.append(AccountUsageBalance(label="Credits", remaining=float(balance), currency="USD"))
    elif credits.get("has_credits") and credits.get("unlimited"):
        details.append("Credits balance: unlimited")
    return _snapshot("openai-codex", "usage_api", windows, details, plan=_title_case_slug(payload.get("plan_type")),
                     balances=tuple(balances), allowed=_opt_bool(rate_limit.get("allowed")), limit_reached=limit_reached)


def _pct(value: float) -> float:
    value = float(value)
    return value * 100 if value <= 1 else value


_ANTHROPIC_KNOWN_KEYS = frozenset({"limits", "extra_usage", "five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"})


def anthropic_usage_snapshot(payload: dict) -> AccountUsageSnapshot:
    """Map the OAuth usage payload: the self-describing ``limits`` list when present (else the legacy
    fixed windows), any extra codenamed ``{utilization,...}`` buckets, and extra-usage credits."""
    windows: list[AccountUsageWindow] = []
    raw_limits = payload.get("limits")
    if isinstance(raw_limits, list):
        for raw in raw_limits:
            if not isinstance(raw, dict) or not _is_num(used := raw.get("percent", raw.get("utilization"))):
                continue
            scope = raw.get("scope")
            if isinstance(scope, dict):
                scope = scope.get("model") or scope.get("name")
            windows.append(AccountUsageWindow(
                label=_title_case_slug(raw.get("kind")) or "Usage limit", used_percent=_pct(used),
                reset_at=_parse_dt(raw.get("resets_at")),
                severity=str(raw["severity"]) if raw.get("severity") is not None else None,
                is_active=_opt_bool(raw.get("is_active")), scope=str(scope) if scope else None,
                limit_reached=_opt_bool(raw.get("limit_reached"))))
    else:
        windows = _usage_windows(
            payload, (("five_hour", "Current session"), ("seven_day", "Current week"), ("seven_day_opus", "Opus week"),
                      ("seven_day_sonnet", "Sonnet week")), "utilization", "resets_at", fraction=True)
    details: list[str] = []
    balances: list[AccountUsageBalance] = []
    for key, raw in payload.items():
        if key in _ANTHROPIC_KNOWN_KEYS or not isinstance(raw, dict) or not _is_num(raw.get("utilization")):
            continue
        label = _title_case_slug(key) or "Usage limit"
        windows.append(AccountUsageWindow(
            label=label, used_percent=_pct(raw["utilization"]), reset_at=_parse_dt(raw.get("resets_at")),
            severity=str(raw["severity"]) if raw.get("severity") is not None else None,
            is_active=_opt_bool(raw.get("is_active"))))
        # Some codenamed windows are denominated in dollars rather than percent; carry those through
        # so a plan whose only meaningful signal is a dollar figure still shows something real.
        dollars = {f: (float(raw[f]) if _is_num(raw.get(f)) else None)
                   for f in ("used_dollars", "limit_dollars", "remaining_dollars")}
        if any(v is not None for v in dollars.values()):
            balances.append(AccountUsageBalance(label=label, used=dollars["used_dollars"], limit=dollars["limit_dollars"],
                                                remaining=dollars["remaining_dollars"], currency="USD"))
    # Extra usage is a real bucket on its own: some plans (managed/work accounts) expose NO session/
    # weekly limits, only this credit pool. Map whatever the provider actually sent and omit the rest.
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used_credits, monthly_limit, currency = extra.get("used_credits"), extra.get("monthly_limit"), str(extra.get("currency") or "USD")
        has_used, has_limit = _is_num(used_credits), _is_num(monthly_limit)
        used_percent: Optional[float] = None
        if _is_num(extra.get("utilization")):
            used_percent = _pct(extra["utilization"])
        elif has_used and has_limit and float(monthly_limit) > 0:
            used_percent = float(used_credits) / float(monthly_limit) * 100
        if has_used or has_limit or used_percent is not None:
            windows.append(AccountUsageWindow(label="Extra Usage", used_percent=used_percent,
                                              limit_reached=bool(extra.get("spend_limit_reached")) or None))
            balances.append(AccountUsageBalance(
                label="Extra Usage", used=float(used_credits) if has_used else None,
                limit=float(monthly_limit) if has_limit else None,
                remaining=max(0.0, float(monthly_limit) - float(used_credits)) if has_used and has_limit else None,
                currency=currency))
            if has_used and has_limit:
                details.append(f"Extra usage: {float(used_credits):.2f} / {float(monthly_limit):.2f} {currency}")
            elif has_used:
                details.append(f"Extra usage: {float(used_credits):.2f} {currency} used")
    return _snapshot("anthropic", "oauth_usage_api", windows, details, balances=tuple(balances))
