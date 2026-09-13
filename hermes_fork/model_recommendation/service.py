"""Profile-scoped, read-only model recommendation contract for Desktop.

The service deliberately has no access to sessions or transcript storage.  Its
only user content is the one unsent draft supplied by the caller, and that
content is passed once to a dedicated configured router without logging or
persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

POLICIES = frozenset({"balanced", "save_codex", "best_quality"})
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_AVAILABILITY_FRESH_SECONDS = 900
_UNAVAILABLE = "Model recommendation router is not configured."

_OUTPUT_SCHEMA = {
    "name": "model_recommendations",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_risk", "ambiguous", "recommendations"],
        "properties": {
            "task_risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "ambiguous": {"type": "boolean"},
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["provider", "model", "effort", "reason", "quality", "materially_advantageous"],
                    "properties": {
                        "provider": {"type": "string"},
                        "model": {"type": "string"},
                        "effort": {"type": "string", "enum": list(EFFORTS)},
                        "reason": {"type": "string", "maxLength": 240},
                        # No minimum/maximum: Anthropic's structured-output validator 400s on
                        # numeric bounds ("properties maximum, minimum are not supported").
                        # The 0-100 bound is enforced in _parse_router_output instead.
                        "quality": {"type": "integer"},
                        "materially_advantageous": {"type": "boolean"},
                    },
                },
            },
        },
    },
}


def unavailable(reason: str = _UNAVAILABLE) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "recommendations": []}


def _router_config(config: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        return None
    auxiliary = config.get("auxiliary")
    raw = auxiliary.get("model_recommendation") if isinstance(auxiliary, dict) else None
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    # "auto" would implicitly select a provider or start the aux fallback ladder.
    if not provider or provider.lower() == "auto" or not model:
        return None
    try:
        timeout = max(1, min(int(raw.get("timeout") or 30), 120))
    except (TypeError, ValueError):
        return None
    return {
        "provider": provider,
        "model": model,
        "base_url": str(raw.get("base_url") or "").strip() or None,
        "api_key": str(raw.get("api_key") or "").strip() or None,
        "api_mode": str(raw.get("api_mode") or "").strip() or None,
        "timeout": timeout,
    }


def _safe_attachment_metadata(attachments: Any) -> list[dict[str, Any]]:
    if not isinstance(attachments, list):
        raise ValueError("attachments must be a list of metadata objects")
    safe: list[dict[str, Any]] = []
    for item in attachments[:32]:
        if not isinstance(item, dict):
            raise ValueError("attachments must contain metadata objects")
        entry: dict[str, Any] = {}
        for key in ("name", "mime_type", "size", "kind"):
            value = item.get(key)
            if isinstance(value, str) and len(value) <= 256:
                entry[key] = value
            elif isinstance(value, int) and 0 <= value <= 2**53:
                entry[key] = value
        safe.append(entry)
    return safe


def _availability_payload(providers: set[str]) -> dict[str, dict[str, Any]]:
    supported = tuple(sorted(providers & {"anthropic", "openai-codex"}))
    if not supported:
        return {}
    try:
        from hermes_fork.account_limits.service import fetch_account_limits, serialize_account_usage
        snapshots = fetch_account_limits(supported)
    except Exception:
        return {provider: {"status": "failed"} for provider in supported}
    result: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    for snapshot in snapshots:
        item = serialize_account_usage(snapshot)
        fetched_at = item.get("fetched_at")
        try:
            age = (now - datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))).total_seconds()
        except Exception:
            age = _AVAILABILITY_FRESH_SECONDS + 1
        if item.get("unavailable_reason"):
            status = "unavailable"
        elif age > _AVAILABILITY_FRESH_SECONDS:
            status = "stale"
        else:
            status = "fresh"
        result[str(item.get("provider") or "").lower()] = {
            "status": status,
            "allowed": item.get("allowed"),
            "limit_reached": item.get("limit_reached"),
        }
    return result


def discover_eligible_candidates() -> list[dict[str, Any]]:
    """Return validated configured/authenticated inventory routes, never credentials."""
    from hermes_cli.inventory import build_model_options_payload, load_picker_context
    from hermes_cli.providers import HERMES_OVERLAYS

    context = load_picker_context()
    payload = build_model_options_payload(context, explicit_only=True)
    configured_providers = {
        str(provider).strip().lower() for provider in context.user_providers
        if str(provider).strip()
    }
    candidates: list[dict[str, Any]] = []
    for row in payload.get("providers") or []:
        if not isinstance(row, dict) or row.get("authenticated") is not True:
            continue
        provider = str(row.get("slug") or "").strip().lower()
        if not provider or provider == "moa":
            continue
        # The Desktop picker permits zero-setup/keyless rows, but an advisory router must only
        # assess a provider the profile explicitly configured.
        if getattr(HERMES_OVERLAYS.get(provider), "keyless", False) and provider not in configured_providers:
            continue
        unavailable_models = {
            model for model in (row.get("unavailable_models") or [])
            if isinstance(model, str)
        }
        capabilities = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        for model in row.get("models") or []:
            if not isinstance(model, str) or not model.strip() or model in unavailable_models:
                continue
            caps = capabilities.get(model) if isinstance(capabilities.get(model), dict) else {}
            price = pricing.get(model) if isinstance(pricing.get(model), dict) else {}
            reasoning = bool(caps.get("reasoning", False))
            candidates.append({
                "provider": provider,
                "model": model,
                "capabilities": {
                    "reasoning": reasoning,
                    "fast": bool(caps.get("fast", False)),
                    "effort_options": list(EFFORTS if reasoning else ("none",)),
                },
                "cost": "free" if price.get("free") is True else "paid_or_unknown",
            })
    return candidates


def _router_messages(draft: str, attachments: list[dict[str, Any]], policy: str,
                     candidates: list[dict[str, Any]], availability: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    instruction = (
        "Choose at most one route per provider from the supplied candidates. Return only the strict JSON schema. "
        "Use the complete unsent draft and attachment metadata, not hidden context. If the request is ambiguous, "
        "set ambiguous=true and choose a conservative higher effort with a concise reason. "
        f"Policy: {policy}. balanced means cheapest adequate route; save_codex preserves Codex capacity unless "
        "materially advantageous; best_quality means strongest eligible route with effort scaled to risk."
    )
    request = {"draft": draft, "attachments": attachments, "candidates": candidates, "availability": availability}
    return [{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps(request, separators=(",", ":"))}]


def _run_router_once(router: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """Make exactly one direct provider call: no auto route, retry, or fallback ladder."""
    from agent.auxiliary_client import _endpoint_speaks_anthropic_messages, resolve_provider_client

    client, resolved_model = resolve_provider_client(
        router["provider"], model=router["model"], explicit_base_url=router["base_url"],
        explicit_api_key=router["api_key"], api_mode=router["api_mode"], task="model_recommendation")
    if client is None or not resolved_model:
        raise RuntimeError("router provider is unavailable")
    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 900,
        "timeout": router["timeout"],
        "response_format": {"type": "json_schema", "json_schema": _OUTPUT_SCHEMA},
    }
    # Adaptive-thinking Claude models (Sonnet/Opus/Fable 4.6+) think by default even with no
    # reasoning config, and that invisible thinking counts against max_tokens. This is a fixed,
    # cheap, deterministic classification call (temperature=0) with no need for chain-of-thought,
    # so disable it explicitly — otherwise thinking alone can consume the whole 900-token budget
    # and truncate the JSON answer before it is written (finish_reason="length" -> unparseable
    # output -> always "unavailable" on Claude, regardless of the schema/candidates being fine).
    # ``_reasoning_config`` is a private kwarg only Anthropic-Messages-wire adapters understand
    # (agent/auxiliary_client.py's own _prepare_aux_request gates it the same way); a bare
    # OpenAI-compatible client (custom/openrouter/xai-oauth routers) would 400/TypeError on it.
    if router["provider"] == "anthropic" or _endpoint_speaks_anthropic_messages(router["base_url"] or ""):
        kwargs["_reasoning_config"] = {"enabled": False}
    response = client.chat.completions.create(**kwargs)
    return str(response.choices[0].message.content or "")


def _parse_router_output(raw: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"task_risk", "ambiguous", "recommendations"}:
        return None
    if data.get("task_risk") not in {"low", "medium", "high"} or not isinstance(data.get("ambiguous"), bool):
        return None
    recommendations = data.get("recommendations")
    if not isinstance(recommendations, list):
        return None
    routes = {(item["provider"], item["model"]): item for item in candidates}
    seen: set[str] = set()
    checked: list[dict[str, Any]] = []
    for item in recommendations:
        if not isinstance(item, dict) or set(item) != {"provider", "model", "effort", "reason", "quality", "materially_advantageous"}:
            return None
        provider, model, effort = item.get("provider"), item.get("model"), item.get("effort")
        if (not isinstance(provider, str) or not isinstance(model, str) or provider in seen
                or (provider, model) not in routes or effort not in EFFORTS
                or not isinstance(item.get("reason"), str) or len(item["reason"]) > 240
                or type(item.get("quality")) is not int or not 0 <= item["quality"] <= 100
                or not isinstance(item.get("materially_advantageous"), bool)):
            return None
        candidate = routes[(provider, model)]
        if effort not in candidate["capabilities"]["effort_options"]:
            return None
        seen.add(provider)
        checked.append({**item, "capabilities": candidate["capabilities"], "cost": candidate["cost"]})
    if seen != {candidate["provider"] for candidate in candidates}:
        return None
    return {"task_risk": data["task_risk"], "ambiguous": data["ambiguous"], "recommendations": checked}


def _conservative_effort(item: dict[str, Any], risk: str, ambiguous: bool) -> str:
    options = item["capabilities"]["effort_options"]
    if not ambiguous:
        return item["effort"]
    desired = {"low": "medium", "medium": "high", "high": "xhigh"}[risk]
    return next((effort for effort in options if EFFORTS.index(effort) >= EFFORTS.index(desired)), options[-1])


def rank_recommendations(parsed: dict[str, Any], availability: dict[str, dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in parsed["recommendations"]:
        availability_state = availability.get(item["provider"], {"status": "unsupported"})
        effort = _conservative_effort(item, parsed["task_risk"], parsed["ambiguous"])
        reason = item["reason"]
        if parsed["ambiguous"]:
            reason = f"{reason} Draft-only assessment is ambiguous; effort was raised conservatively."
        prepared.append({
            "provider": item["provider"], "model": item["model"], "effort": effort,
            "reason": reason, "capabilities": item["capabilities"], "availability": availability_state,
            "quality": item["quality"], "cost": item["cost"], "materially_advantageous": item["materially_advantageous"],
        })

    def sort_key(item: dict[str, Any]) -> tuple:
        unavailable = item["availability"].get("status") in {"unavailable", "failed", "stale"}
        cost = 0 if item["cost"] == "free" else 1
        codex_penalty = 1 if (policy == "save_codex" and item["provider"] == "openai-codex"
                              and not item["materially_advantageous"]) else 0
        if policy == "best_quality":
            return (unavailable, -item["quality"], cost, item["provider"])
        if policy == "save_codex":
            return (unavailable, codex_penalty, cost, -item["quality"], item["provider"])
        return (unavailable, cost, -item["quality"], item["provider"])

    ranked = sorted(prepared, key=sort_key)
    return [{key: value for key, value in item.items() if key not in {"quality", "cost", "materially_advantageous"}}
            for item in ranked]


def recommend(*, draft: Any, attachments: Any, policy: Any) -> dict[str, Any]:
    if not isinstance(draft, str) or not draft.strip() or len(draft) > 100_000:
        raise ValueError("draft must be a non-empty string up to 100000 characters")
    policy = str(policy or "balanced").strip().lower()
    if policy not in POLICIES:
        raise ValueError("policy must be balanced, save_codex, or best_quality")
    from hermes_cli.config import load_config
    router = _router_config(load_config())
    if router is None:
        return unavailable()
    safe_attachments = _safe_attachment_metadata(attachments)
    candidates = discover_eligible_candidates()
    if not candidates:
        return unavailable("No configured authenticated model routes are eligible.")
    availability = _availability_payload({item["provider"] for item in candidates})
    try:
        raw = _run_router_once(router, _router_messages(draft, safe_attachments, policy, candidates, availability))
        parsed = _parse_router_output(raw, candidates)
    except Exception:
        return unavailable("Model recommendation router is unavailable.")
    if parsed is None:
        return unavailable("Model recommendation router returned invalid structured output.")
    return {"status": "ok", "policy": policy, "recommendations": rank_recommendations(parsed, availability, policy)}
