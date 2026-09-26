"""Provider capacity adapters for Kanban admission control.

:mod:`agent.kanban_throttle` needs one question answered per provider: *how
much of this account's quota is already spent, right now, according to the
provider itself?*  This module is the only place that knows which providers can
answer it and how.

The contract is deliberately narrow, because the failure mode being designed
against is a confident wrong number:

* **Authenticated machine-readable signals only.**  Every supported adapter
  returns an :class:`~agent.account_usage.AccountUsageSnapshot` built from a
  response the provider itself served to a real credential.  Local token
  counting, pricing tables, plan names and request tallies are NOT capacity —
  they measure something else and would silently invent pressure.

* **Unsupported is a first-class, evidenced answer.**  A provider with no
  trustworthy signal is recorded here as unsupported with the evidence for that
  verdict, and reads back as the ``unsupported_provider`` reason code.  It never
  degrades into a guess, an estimate or a zero.

* **Nothing identifying leaves this module.**  Adapters return snapshots
  carrying window labels, percentages and reset timestamps.  Tokens, account
  identifiers and raw provider payloads stay inside the fetch.

Capability verdicts are recorded in :data:`_CAPABILITIES` with the evidence
behind each one.  Providers not named there but registered in
``agent.account_usage._USAGE_FETCHERS`` are supported through that generic
path, so adding a fetcher upstream does not silently read as unsupported here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# --- Reason codes -------------------------------------------------------

#: The provider has no trustworthy capacity contract at all.  Distinct from a
#: fetch that failed: retrying will not help and the operator needs to know the
#: difference between "temporarily unreadable" and "never readable".
UNSUPPORTED_PROVIDER = "unsupported_provider"

#: A Nous reading reconstructed from cached JWT claims rather than a live
#: account read.  The claims carry entitlement, not current balance, so they
#: cannot answer "how much is left".
STALE_PORTAL_READING = "stale_portal_reading"

#: Signal kinds, for operator-facing diagnostics.
SIGNAL_RATE_LIMIT_WINDOWS = "rate_limit_windows"
SIGNAL_CREDIT_BALANCE = "credit_balance"

#: Unavailability codes THIS module mints, and which are therefore safe to
#: carry verbatim into a signal and a persisted audit row.  An adapter also
#: puts free-form provider prose in ``unavailable_reason`` (it is the operator-
#: facing text ``/usage`` prints), so membership is deliberately exact-match
#: against codes minted here: anything else generalizes rather than being
#: echoed into a durable record.
_STATIC_UNAVAILABLE_REASONS = frozenset({STALE_PORTAL_READING})


def classify_unavailable_reason(reason: Optional[str]) -> Optional[str]:
    """This module's own sanitized code for *reason*, or ``None``.

    A snapshot's ``unavailable_reason`` is two different things wearing one
    field: an enumerated code minted here, or arbitrary provider/adapter prose.
    Collapsing both into a generic verdict loses a distinction the operator
    needs — ``stale_portal_reading`` means "log in again", not "the provider is
    down" — while echoing both would put untrusted text in the audit trail.
    Recognising only what this module minted keeps each named case named and
    everything else generic.
    """
    text = str(reason or "").strip()
    return text if text in _STATIC_UNAVAILABLE_REASONS else None


@dataclass(frozen=True)
class ProviderCapability:
    """What is actually knowable about one provider's remaining capacity."""

    provider: str
    supported: bool
    #: Where a supported reading comes from, or ``None`` when unsupported.
    source: Optional[str]
    #: What the number MEANS: a provider-enforced rate-limit window, or a
    #: prepaid credit balance.  Both are real capacity; they are not the same
    #: thing and an operator reading a diagnostic should not have to guess.
    signal: Optional[str]
    #: Why this verdict holds.  For an unsupported provider this is the whole
    #: justification for refusing to produce a number, so it names what was
    #: probed rather than asserting a conclusion.
    evidence: str

    def public_state(self) -> dict[str, Any]:
        """Sanitized capability record.  Contains no credential, account
        identifier or raw provider payload — only this module's own verdict."""
        return {
            "provider": self.provider,
            "supported": self.supported,
            "source": self.source,
            "signal": self.signal,
            "evidence": self.evidence,
        }


#: Spellings an operator might reasonably write, mapped to the canonical
#: provider key.  Kept small and explicit: a fuzzy match here would quietly
#: route one account's quota onto another account's decisions.
_ALIASES = {
    "grok": "xai",
    "x-ai": "xai",
    "x.ai": "xai",
    "grok-oauth": "xai-oauth",
    "x-ai-oauth": "xai-oauth",
    "claude": "anthropic",
    "codex": "openai-codex",
    "openai_codex": "openai-codex",
    "nousresearch": "nous",
}


def normalize_provider(provider: Optional[str]) -> str:
    key = str(provider or "").strip().lower()
    return _ALIASES.get(key, key)


_XAI_EVIDENCE = (
    "No quota document is exposed to an xAI inference credential: with a "
    "resolved OAuth token, GET api.x.ai/v1/{usage,billing/usage,rate-limits,"
    "credits} return 404 and /v1/api-key returns 401. management-api.x.ai "
    "requires a separate management key and serves team/ACL management, not "
    "consumption. Remaining quota is observable only reactively, via 429s on "
    "inference calls, which is a reaction to exhaustion rather than the "
    "advance capacity reading admission control needs."
)

_CAPABILITIES: dict[str, ProviderCapability] = {
    "anthropic": ProviderCapability(
        provider="anthropic", supported=True, source="oauth_usage_api",
        signal=SIGNAL_RATE_LIMIT_WINDOWS,
        evidence=(
            "OAuth /api/oauth/usage returns self-describing limit windows with "
            "per-window utilization, reset timestamps and an is_active flag."
        ),
    ),
    "openai-codex": ProviderCapability(
        provider="openai-codex", supported=True, source="usage_api",
        signal=SIGNAL_RATE_LIMIT_WINDOWS,
        evidence=(
            "The Codex backend /usage endpoint returns primary/secondary "
            "rate-limit windows with used_percent and reset_at, plus explicit "
            "allowed / limit_reached blocked-state metadata."
        ),
    ),
    "nous": ProviderCapability(
        provider="nous", supported=True, source="portal_account_api",
        signal=SIGNAL_CREDIT_BALANCE,
        evidence=(
            "The Portal account API returns the subscription's monthly_credits "
            "and credits_remaining for the live billing period, an "
            "authenticated consumption reading rather than a local estimate. "
            "Only a fresh account_api read counts; a reading reconstructed "
            "from cached JWT claims carries entitlement, not balance."
        ),
    ),
    "xai": ProviderCapability(
        provider="xai", supported=False, source=None, signal=None,
        evidence=_XAI_EVIDENCE,
    ),
    "xai-oauth": ProviderCapability(
        provider="xai-oauth", supported=False, source=None, signal=None,
        evidence=_XAI_EVIDENCE,
    ),
}


def _generic_capability(provider: str) -> Optional[ProviderCapability]:
    """Capability for a provider that ``account_usage`` can already fetch.

    Derived from the live fetcher registry rather than a second hardcoded list,
    so a provider gaining an upstream fetcher does not read as unsupported here
    until someone remembers to update a table.
    """
    try:
        from agent.account_usage import _USAGE_FETCHERS
    except Exception:  # pragma: no cover - import failure means no capability
        logger.debug("capacity: usage fetcher registry unreadable", exc_info=True)
        return None
    if provider not in _USAGE_FETCHERS:
        return None
    return ProviderCapability(
        provider=provider, supported=True, source="account_usage_fetcher",
        signal=SIGNAL_RATE_LIMIT_WINDOWS,
        evidence=(
            "Registered in agent.account_usage._USAGE_FETCHERS: an "
            "authenticated snapshot is fetched through the shared provider "
            "path. A reading with no usable window is reported as degraded, "
            "never as zero pressure."
        ),
    )


def capability_for(provider: Optional[str]) -> ProviderCapability:
    """This provider's capacity contract.  Never raises, never returns None."""
    key = normalize_provider(provider)
    known = _CAPABILITIES.get(key)
    if known is not None:
        return known
    generic = _generic_capability(key)
    if generic is not None:
        return generic
    return ProviderCapability(
        provider=key, supported=False, source=None, signal=None,
        evidence=(
            "No authenticated capacity adapter is registered for this "
            "provider and agent.account_usage has no fetcher for it, so no "
            "trustworthy remaining-quota reading can be obtained."
        ),
    )


def capability_table() -> tuple[ProviderCapability, ...]:
    """Every explicitly recorded verdict, for docs and diagnostics."""
    return tuple(_CAPABILITIES[k] for k in sorted(_CAPABILITIES))


# --- Nous adapter -------------------------------------------------------

#: The only Nous reading that reflects current balance.  ``jwt`` is
#: reconstructed from cached token claims and ``inference_key`` carries no
#: account at all, so neither can answer how much credit is left.
_NOUS_LIVE_SOURCE = "account_api"


def _nous_unavailable(reason: str):
    from agent.account_usage import AccountUsageSnapshot, _utc_now

    return AccountUsageSnapshot(
        provider="nous", source="portal_account_api", fetched_at=_utc_now(),
        title="Nous credits", unavailable_reason=reason,
    )


def nous_capacity_snapshot() -> Optional[Any]:
    """Nous Portal credit balance as a capacity snapshot, or ``None``.

    Mirrors the gauge guards in
    :func:`agent.account_usage.build_nous_credits_snapshot` deliberately: a
    positive finite cap, a finite remaining, and remaining <= cap.  Rollover
    credit can push remaining ABOVE the monthly cap, which makes the cap a
    meaningless denominator — the percentage it would produce is not a capacity
    reading, so no window is emitted and the caller degrades rather than acting
    on it.

    ``paid_service_access is False`` is the Portal's own statement that access
    is depleted, so it is encoded as a fully-consumed window.  That is the
    provider's assertion faithfully carried through, not an estimate derived
    from local accounting.

    The caller bounds wall-clock time; this does not impose its own timeout.
    """
    from agent.account_usage import (
        AccountUsageWindow, _is_finite_num, _parse_dt, _snapshot,
    )

    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        info = get_nous_portal_account_info(force_fresh=True)
    except Exception:
        logger.debug("capacity: nous portal fetch failed", exc_info=True)
        return None
    if info is None or not getattr(info, "logged_in", False):
        return None
    if getattr(info, "error", None):
        # A named failure beats None: the operator learns the account is
        # configured but unreadable, rather than assuming it is absent.
        return _nous_unavailable("Nous Portal account read failed.")
    if not getattr(info, "fresh", False) or getattr(info, "source", None) != _NOUS_LIVE_SOURCE:
        return _nous_unavailable(STALE_PORTAL_READING)

    depleted = getattr(info, "paid_service_access", None) is False
    sub = getattr(info, "subscription", None)
    windows: list[AccountUsageWindow] = []
    reset_at = _parse_dt(getattr(sub, "current_period_end", None)) if sub is not None else None
    cap = getattr(sub, "monthly_credits", None) if sub is not None else None
    remaining = getattr(sub, "credits_remaining", None) if sub is not None else None
    if _is_finite_num(cap) and cap > 0 and _is_finite_num(remaining) and remaining <= cap:
        windows.append(AccountUsageWindow(
            label="Subscription credits",
            used_percent=max(0.0, min(100.0, (cap - remaining) / cap * 100.0)),
            reset_at=reset_at, is_active=True, limit_reached=depleted or None,
        ))
    elif depleted:
        # No usable denominator, but the Portal has stated access is gone.
        # Reporting nothing here would read as "unknown" and hold the board at
        # whatever state it was in while the account is provably spent.
        windows.append(AccountUsageWindow(
            label="Subscription credits", used_percent=100.0,
            reset_at=reset_at, is_active=True, limit_reached=True,
        ))
    if not windows:
        return _nous_unavailable(
            "Nous Portal returned no usable credit denominator for this period."
        )
    return _snapshot(
        "nous", "portal_account_api", windows, [], title="Nous credits",
        plan=getattr(sub, "plan", None) if sub is not None else None,
        allowed=(not depleted) or None, limit_reached=depleted or None,
    )


#: Providers whose capacity comes from a fork adapter rather than from
#: ``account_usage.fetch_account_usage``.
_ADAPTERS: dict[str, Callable[[], Optional[Any]]] = {
    "nous": nous_capacity_snapshot,
}


def fetch_capacity_snapshot(provider: Optional[str]) -> Optional[Any]:
    """One provider's authenticated capacity snapshot, or ``None``.

    ``None`` means *this attempt produced nothing* — a failed fetch, a logged-out
    account, an adapter that declined to build a window.  It deliberately does
    NOT mean "unsupported": that verdict is a stable property of the provider,
    answered by :func:`capability_for` without any network call, and the caller
    is expected to consult it first so the two are never conflated.  Routing an
    unsupported provider here anyway still yields ``None`` rather than a guess.

    Never raises: this runs on the dispatch path, where an exception would wedge
    admission for every board.
    """
    capability = capability_for(provider)
    if not capability.supported:
        return None
    adapter = _ADAPTERS.get(capability.provider)
    try:
        if adapter is not None:
            return adapter()
        from agent.account_usage import fetch_account_usage

        return fetch_account_usage(capability.provider)
    except Exception:
        logger.debug(
            "capacity: %s snapshot fetch failed", capability.provider, exc_info=True
        )
        return None


__all__ = [
    "SIGNAL_CREDIT_BALANCE", "SIGNAL_RATE_LIMIT_WINDOWS", "STALE_PORTAL_READING",
    "UNSUPPORTED_PROVIDER", "ProviderCapability", "capability_for",
    "capability_table", "classify_unavailable_reason", "fetch_capacity_snapshot",
    "normalize_provider", "nous_capacity_snapshot",
]
