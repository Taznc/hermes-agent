"""Structured error-surface descriptors for UI clients (Desktop/TUI).

Maps the internal failure taxonomy (``FailoverReason`` values carried in turn
results as ``failure_reason``, or raw exceptions from the turn dispatcher)
onto a small, stable wire descriptor ``{"layer", "code", "retryable"}``.
Layers: provider (model API rejected/failed), endpoint (user-configured
custom/local endpoint transport failure), streaming (SSE dropped mid-turn),
auth, billing (fallback signal; clients usually have a richer
``billing_block``), gateway (local runtime errored), disk (disk full).
Dependency-light and NEVER raises: surfacing diagnostics must not break the
error path it describes. Descriptors are advisory — clients fall back to
string sniffing when absent or partial.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

LAYER_PROVIDER = "provider"
LAYER_ENDPOINT = "endpoint"
LAYER_STREAMING = "streaming"
LAYER_AUTH = "auth"
LAYER_BILLING = "billing"
LAYER_GATEWAY = "gateway"
LAYER_DISK = "disk"

# failure_reason → UI layer. Unlisted reasons fall back to LAYER_PROVIDER:
# every FailoverReason comes from classifying a provider call.
_REASON_TO_LAYER = {
    "auth": LAYER_AUTH, "auth_permanent": LAYER_AUTH, "billing": LAYER_BILLING, "billing_unverified": LAYER_BILLING,
}

# Failures between us and the base_url (not a provider verdict); on a
# custom/local endpoint they point at the user's endpoint config.
_TRANSPORT_REASONS = {"timeout", "ssl_cert_verification"}

# Deterministic for the request — a bare "Retry" repeats the failure. Fallback
# only: current backends stamp the classifier's verdict in ``failure_retryable``.
# Kept in sync with ``classify_api_error``'s retryable=False verdicts.
_NON_RETRYABLE_REASONS = {
    "auth", "auth_permanent", "billing", "billing_unverified", "content_policy_blocked",
    "provider_policy_blocked", "model_not_found", "format_error", "ssl_cert_verification",
}

# Providers whose base_url is user-supplied rather than a known vendor.
_CUSTOM_ENDPOINT_PROVIDERS = {"custom", "local", "llama.cpp", "llamacpp", "ollama", "lmstudio", "vllm"}

# Mid-stream drop markers. Deliberately narrow: our own retry-exhaustion
# summaries plus the OpenAI SDK's stream-abort errors.
_STREAM_DROP_FRAGMENTS = (
    "stream connection", "peer closed connection", "incomplete chunked read",
    "connection broken", "stream ended prematurely", "sse", "mid-stream",
)

# Exception top-level modules that mean "API/transport call failed" (vs. a bug
# in our dispatcher = gateway layer): every SDK family our adapters raise from
# plus raw transports.
_API_EXC_MODULE_PREFIXES = (
    "openai", "httpx", "httpcore", "anthropic", "botocore", "boto3", "google",
    "grpc", "requests", "aiohttp", "ssl", "socket", "urllib",
)


def _is_custom_endpoint(provider: Optional[str]) -> bool:
    p = (provider or "").strip().lower()
    return p in _CUSTOM_ENDPOINT_PROVIDERS or p.startswith("custom:")


def _looks_like_stream_drop(message: str) -> bool:
    return any(fragment in message.lower() for fragment in _STREAM_DROP_FRAGMENTS)


# Reasons for which the optional ``reset_at`` / ``fallback_available`` wire fields are meaningful
# (Phase 2.12): a true provider-side rate limit, either the caller's own account bucket or an
# upstream-aggregator throttle. Every other reason omits both keys — a billing wall or auth failure
# has no "resets at X" semantics, and stamping the fields there would mislead a client into offering
# a wait/countdown action that never resolves.
_RATE_LIMIT_RESET_REASONS = {"rate_limit", "upstream_rate_limit"}


def _surface(
    layer: str, code: str, retryable: bool, provider: str = "", model: str = "",
    reset_at: Optional[float] = None, fallback_available: Optional[bool] = None,
) -> dict:
    # Identity captured at classification time, so clients report the session
    # that actually failed — not whatever the composer points at later.
    identity = {k: v for k, v in (("provider", provider), ("model", model)) if v}
    out = {"layer": layer, "code": code, "retryable": bool(retryable), **identity}
    # Phase 2.12: optional rate-limit recovery hints. Both are omitted (never set to None/0) when
    # the caller has no usable data — omission is a distinct, meaningful state from an explicit
    # false/0 on the wire (see _RATE_LIMIT_RESET_REASONS).
    if code in _RATE_LIMIT_RESET_REASONS and reset_at is not None:
        try:
            out["reset_at"] = float(reset_at)
        except (TypeError, ValueError):
            pass
    if fallback_available is not None:
        out["fallback_available"] = bool(fallback_available)
    return out


def _recovery_hints(result: dict) -> tuple[Optional[float], Optional[bool]]:
    """``(reset_at, fallback_available)`` threaded through from the terminal-failure result dict
    (agent/conversation_loop.py ``_resolve_rate_limit_reset_at`` / ``_fallback_availability``).
    Wire-optional: an older backend never sets these keys, and ``_surface`` re-gates ``reset_at``
    on a rate-limit code so a stray value can never leak onto an unrelated failure."""
    reset_at = result.get("reset_at")
    if not isinstance(reset_at, (int, float)) or isinstance(reset_at, bool):
        reset_at = None
    fallback_available = result.get("fallback_available")
    if not isinstance(fallback_available, bool):
        fallback_available = None
    return reset_at, fallback_available


def _disk_full(candidate: Any) -> bool:
    try:
        from hermes_state_errors import is_disk_full_error

        return bool(is_disk_full_error(candidate))
    except Exception:  # pragma: no cover - defensive import guard
        return False


def _result_layer(reason: str, error_text: str, provider: str) -> str:
    if reason in _REASON_TO_LAYER:
        return _REASON_TO_LAYER[reason]
    if reason in _TRANSPORT_REASONS and _is_custom_endpoint(provider):
        return LAYER_ENDPOINT
    return LAYER_STREAMING if _looks_like_stream_drop(error_text) else LAYER_PROVIDER


def build_error_surface_from_result(result: Any, provider: str = "", model: str = "") -> Optional[dict]:
    """Descriptor for a returned-error turn result (``failed=True`` dicts).

    Uses the stamped ``failure_reason`` plus error text. None when the result
    carries no failure signal.
    """
    try:
        if not isinstance(result, dict):
            return None
        error_text = str(result.get("error") or "")
        reason = str(result.get("failure_reason") or "").strip()
        if not error_text and not reason:
            return None
        reset_at, fallback_available = _recovery_hints(result)
        # Disk-full wins outright: the fix (free space) is unrelated to the
        # provider stack; hermes_state owns the pattern list.
        if error_text and _disk_full(error_text):
            return _surface(LAYER_DISK, "disk_full", False, provider, model)
        if result.get("billing_block") or reason in ("billing", "billing_unverified"):
            return _surface(
                LAYER_BILLING, reason or "billing", False, provider, model,
                reset_at=reset_at, fallback_available=fallback_available)
        if not reason:  # failed result without a classified reason (legacy paths)
            drop = _looks_like_stream_drop(error_text)
            return _surface(LAYER_STREAMING if drop else LAYER_PROVIDER, "stream_drop" if drop else "unknown", True, provider, model)
        # Prefer the classifier's own verdict (``failure_retryable``); the
        # reason-set fallback covers older results.
        retryable = result.get("failure_retryable")
        if not isinstance(retryable, bool):
            retryable = reason not in _NON_RETRYABLE_REASONS
        return _surface(
            _result_layer(reason, error_text, provider), reason, retryable, provider, model,
            reset_at=reset_at, fallback_available=fallback_available)
    except Exception:  # pragma: no cover — never break the error path
        logger.debug("error_surface: result classification failed", exc_info=True)
        return None


def build_error_surface_from_exception(
    exc: BaseException, provider: str = "", model: str = "", *, fallback_available: Optional[bool] = None,
) -> Optional[dict]:
    """Descriptor for an exception that escaped the turn dispatcher.

    API/transport exceptions go through ``classify_api_error`` (same taxonomy
    as the retry loop); anything else is a gateway-layer failure.

    ``fallback_available``: exception-path callers (e.g. tui_gateway) have the ``agent`` and its
    ``_fallback_chain`` / ``_fallback_index`` state (``agent.conversation_loop._fallback_availability``
    is the same tri-state logic used on the result-dict path); pass it through when known. Left
    unset when the caller has no such visibility — never guessed.
    """
    try:
        message = str(exc) or type(exc).__name__
        if _disk_full(exc):
            return _surface(LAYER_DISK, "disk_full", False, provider, model)
        api_like = (type(exc).__module__ or "").split(".")[0] in _API_EXC_MODULE_PREFIXES or hasattr(exc, "status_code")
        if not api_like or not isinstance(exc, Exception):
            return _surface(LAYER_GATEWAY, type(exc).__name__, True, provider, model)

        from agent.error_classifier import classify_api_error

        classified = classify_api_error(exc, provider=provider, model=model)
        synthetic: dict[str, Any] = {"error": classified.message or message, "failure_reason": classified.reason.value}
        # The classifier stamps a best-effort reset time into error_context for rate_limit /
        # upstream_rate_limit verdicts (agent/error_classifier.py ``_extract_reset_epoch_seconds``);
        # carry it through the same synthetic-result path as the returned-error case.
        try:
            _ctx_reset = (getattr(classified, "error_context", None) or {}).get("reset_at")
            if isinstance(_ctx_reset, (int, float)) and not isinstance(_ctx_reset, bool):
                synthetic["reset_at"] = float(_ctx_reset)
        except Exception:
            pass
        if fallback_available is not None:
            synthetic["fallback_available"] = bool(fallback_available)
        surface = build_error_surface_from_result(synthetic, provider=provider, model=model)
        if surface is not None:
            surface["retryable"] = bool(classified.retryable)
        return surface
    except Exception:  # pragma: no cover — never break the error path
        logger.debug("error_surface: exception classification failed", exc_info=True)
        return None


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

LAYER_RUNTIME = "runtime"
# ---- END PLUGIN-COMPAT ----
