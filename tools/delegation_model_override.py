"""Per-spawn model / reasoning-effort resolution for ``delegate_task``.

``delegate_task`` children historically inherit the parent's model and
fallback chain unconditionally, and the only override is the GLOBAL
``delegation.provider`` / ``delegation.model`` / ``delegation.reasoning_effort``
pin in ``config.yaml``. That pin applies to every delegation, so a batch that
mixes one hard reasoning task with several mechanical ones must run them all on
one model at one depth.

This module resolves the optional per-spawn ``model`` / ``reasoning_effort``
arguments. The precedence is fixed and identical for both fields:

    per-spawn argument  >  global ``delegation.*`` pin  >  parent inheritance

Two design points matter and are deliberate:

* **A per-spawn model is expressed as a synthetic delegation-config dict** and
  then run through the SAME ``_resolve_delegation_credentials()`` the global
  pin uses. Credential scoping therefore comes for free and cannot drift: a
  child can only ever select a model on a provider the profile already has
  working credentials for, because that resolver is the one that raises when
  a provider has no key.
* **Catalog validation only rejects on positive evidence.** An unknown model
  is rejected with a truthful error naming the provider whose catalog was
  searched — but if the catalog is empty or unavailable (custom endpoint,
  offline Ollama probe, network failure), the model is ACCEPTED rather than
  falsely rejected. Refusing a legitimate spawn because a catalog fetch failed
  is worse than letting the provider return its own error.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Providers whose "catalog" is inherently open-ended: any string can be a valid
# model id, so a membership test carries no information and must not reject.
_OPEN_CATALOG_PROVIDERS = frozenset(
    {"custom", "ollama", "lmstudio", "llamacpp", "llama-cpp", "vllm", "openai-compatible"}
)

__all__ = [
    "resolve_effort_override",
    "resolve_model_override",
    "describe_route",
]


# ── reasoning effort ─────────────────────────────────────────────────────

def resolve_effort_override(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve a per-spawn ``reasoning_effort`` value.

    Returns ``(reasoning_config, error)``:

    * ``(None, None)``   — field omitted; caller keeps existing behaviour
      (global ``delegation.reasoning_effort`` pin, else parent inheritance).
    * ``(cfg, None)``    — parsed config dict to hand to the child agent.
      ``{"enabled": False}`` for ``"none"``; ``{"enabled": True,
      "effort": <level>}`` otherwise.
    * ``(None, error)``  — unrecognised level; the spawn must fail loudly.

    Unlike the global pin (which WARNS and inherits on a bad value, because a
    config typo should not break every delegation), an explicit per-spawn
    argument that cannot be honoured is an error: the caller asked for a
    specific depth and silently running at another one is a lie.
    """
    if raw is None:
        return None, None
    # A bare empty string means "not set" from lenient callers; treat as omitted.
    if isinstance(raw, str) and not raw.strip():
        return None, None

    from hermes_constants import parse_reasoning_effort, VALID_REASONING_EFFORTS

    parsed = parse_reasoning_effort(raw)
    if parsed is None:
        valid = ", ".join(sorted(VALID_REASONING_EFFORTS)) + ", none"
        return None, (
            f"Unknown reasoning_effort {raw!r}. Valid levels: {valid}."
        )
    return parsed, None


# ── model ────────────────────────────────────────────────────────────────

def _parent_provider(parent_agent) -> str:
    return (getattr(parent_agent, "provider", None) or "").strip().lower()


def _catalog_for(provider: str) -> list:
    """Best-effort model catalog for a provider. Empty list on any failure."""
    if not provider or provider in _OPEN_CATALOG_PROVIDERS:
        return []
    try:
        from hermes_cli.models import provider_model_ids

        return list(provider_model_ids(provider) or [])
    except Exception as exc:  # pragma: no cover - catalog is advisory only
        logger.debug("Model catalog unavailable for %r: %s", provider, exc)
        return []


def _in_catalog(model: str, catalog: list) -> bool:
    want = model.strip().lower()
    for entry in catalog:
        entry_l = str(entry).strip().lower()
        if want == entry_l:
            return True
        # Accept a bare name against a ``vendor/model`` catalog id and vice
        # versa — the same tolerance /model auto-detect applies.
        if "/" in entry_l and want == entry_l.split("/", 1)[1]:
            return True
        if "/" in want and want.split("/", 1)[1] == entry_l:
            return True
    return False


def _detect_provider(model: str, current_provider: str) -> Optional[Tuple[str, str]]:
    """Wrap the CLI's model→provider auto-detect; never raises."""
    try:
        from hermes_cli.models import detect_provider_for_model

        return detect_provider_for_model(model, current_provider or "")
    except Exception as exc:  # pragma: no cover - detection is advisory
        logger.debug("Provider auto-detect failed for %r: %s", model, exc)
        return None


def resolve_model_override(
    raw_model: Any,
    parent_agent,
    base_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve a per-spawn ``model`` into a delegation-config-shaped dict.

    Returns ``(cfg, error)``. ``cfg`` is ``None`` when the field was omitted,
    in which case the caller keeps today's behaviour exactly (global pin, else
    parent inheritance). Otherwise ``cfg`` is shaped like the ``delegation``
    config section and is meant to be passed straight to
    ``_resolve_delegation_credentials()``, which performs the actual
    credential resolution and enforces credential scoping.

    Provider selection for the requested model, in order:

    1. The model auto-detects to a provider (static catalog, then OpenRouter).
    2. Else the global ``delegation.provider`` pin, when one is configured —
       the operator pinned a provider and named a model on it.
    3. Else the parent's provider (plain model swap on the same provider).
    """
    if raw_model is None:
        return None, None
    model = str(raw_model).strip()
    if not model:
        return None, None

    cfg = base_cfg if isinstance(base_cfg, dict) else {}
    parent_provider = _parent_provider(parent_agent)
    pinned_provider = str(cfg.get("provider") or "").strip().lower() or None

    resolved_model = model
    resolved_provider: Optional[str] = None

    detected = _detect_provider(model, parent_provider or pinned_provider or "")
    if detected:
        resolved_provider, resolved_model = detected[0], detected[1] or model
    else:
        # No confident cross-provider match. Fall back down the ladder and
        # validate membership against whichever provider we land on.
        resolved_provider = pinned_provider or parent_provider or None
        catalog = _catalog_for(resolved_provider or "")
        if catalog and not _in_catalog(model, catalog):
            sample = ", ".join(str(m) for m in catalog[:8])
            more = f" (+{len(catalog) - 8} more)" if len(catalog) > 8 else ""
            return None, (
                f"Unknown model {model!r} for provider "
                f"{resolved_provider or 'unknown'!r}. It is not in that "
                f"provider's catalog and does not match any other configured "
                f"provider's catalog. Known models include: {sample}{more}. "
                f"Pass a model id from a provider this profile has "
                f"credentials for, or omit 'model' to inherit the parent's."
            )

    # Same provider as the parent AND no global pin → a pure model swap.
    # Return provider=None so _resolve_delegation_credentials takes its
    # inherit-everything branch and the child keeps the parent's credentials,
    # base_url and transport. Going through resolve_runtime_provider here
    # would needlessly re-resolve (and could pick a different base_url than
    # the parent is actually running on).
    if (
        resolved_provider
        and parent_provider
        and resolved_provider == parent_provider
        and not pinned_provider
        and not cfg.get("base_url")
    ):
        return {"model": resolved_model}, None

    override_cfg: Dict[str, Any] = {"model": resolved_model}
    if resolved_provider:
        override_cfg["provider"] = resolved_provider
    # Carry the pin's transport details ONLY when the per-spawn model stays on
    # the pinned provider; a different provider must resolve its own endpoint.
    if pinned_provider and resolved_provider == pinned_provider:
        for key in ("base_url", "api_key", "api_mode", "command", "args"):
            if cfg.get(key):
                override_cfg[key] = cfg[key]
    return override_cfg, None


# ── auditability ─────────────────────────────────────────────────────────

def describe_route(
    creds: Optional[Dict[str, Any]],
    parent_agent,
    source: str = "inherit",
) -> Dict[str, Any]:
    """Describe where a child will actually run, for dispatch/transcript.

    Always reports a concrete model rather than ``null``-on-inherit, so the
    audit trail answers "what did this child run on?" without the reader
    having to know the inheritance rules.

    ``source`` names which precedence level supplied the model — ``"spawn"``
    (per-spawn argument), ``"config"`` (global ``delegation.*`` pin), or
    ``"inherit"`` (parent's model). Reporting the level matters: "not
    inherited" alone cannot distinguish a deliberate per-call route from a
    global pin the caller may not know is set.
    """
    creds = creds or {}
    model = creds.get("model") or getattr(parent_agent, "model", None)
    provider = creds.get("provider") or getattr(parent_agent, "provider", None)
    return {
        "model": str(model) if model else None,
        "provider": str(provider) if provider else None,
        "source": source,
        "inherited": source == "inherit",
    }
