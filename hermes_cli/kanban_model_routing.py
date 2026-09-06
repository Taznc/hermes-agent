"""Kanban create-time model routing.

The resolver is intentionally tiny and fail-closed: it inspects only the card
`title` and `body`, makes at most one classifier call, and falls back to the
profile default whenever the classifier is unavailable, malformed, or selects
an unsupported route.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Optional

from hermes_cli.config import cfg_get, load_config_readonly
from hermes_cli.kanban_db import normalize_reasoning_effort

logger = logging.getLogger(__name__)

try:  # Imported lazily elsewhere too; keep the module usable in tests.
    from agent.auxiliary_client import call_llm_single_attempt as _call_llm
except Exception:  # pragma: no cover - import guard
    _call_llm = None


DEFAULT_CLASSIFIER_MAX_INPUT_TOKENS = 8000
_DEFAULT_ROUTE_NAME = "default"
_SUPPORTED_ROUTES = frozenset({"mechanical"})


@dataclass(frozen=True)
class KanbanModelRouteDecision:
    """Resolved routing for one create operation."""

    route_source: str
    route_name: Optional[str]
    model_override: Optional[str]
    provider_override: Optional[str]
    reasoning_effort: Optional[str]


def _route_is_explicit(model: Optional[str], provider: Optional[str], reasoning_effort: Optional[str]) -> bool:
    return bool(model or provider or reasoning_effort)


def _normalize_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _truncate_for_classifier(text: str, *, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 32:
        return text[:max_chars]
    head = max_chars // 2
    tail = max_chars - head - 5
    return f"{text[:head]}\n…\n{text[-tail:]}"


def _classifier_prompt(title: str, body: Optional[str], *, max_input_tokens: int) -> tuple[list[dict[str, str]], int]:
    # The classifier only sees compact card text. The input cap is approximate
    # (token -> chars) but intentionally conservative.
    body_text = _normalize_text(body) or ""
    char_budget = max(512, int(max_input_tokens) * 4)
    title_text = _normalize_text(title) or ""
    title_line = _truncate_for_classifier(title_text, max_chars=min(len(title_text), char_budget))
    remaining = max(0, char_budget - len(title_line) - 2)
    body_line = _truncate_for_classifier(body_text, max_chars=remaining) if body_text else ""
    payload = {
        "title": title_line,
        "body": body_line,
        "instructions": (
            "Decide whether this card is safe for the configured cheap route. "
            "Return JSON with exactly one key named route and a value of either "
            "\"default\" or \"mechanical\". Use mechanical only for narrowly scoped, "
            "deterministic, low-risk text/code maintenance work."
        ),
    }
    system = (
        "You are a conservative Kanban routing classifier. "
        "Return only valid JSON. "
        "Never invent routes other than default or mechanical."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], char_budget


def _parse_classifier_route(content: Any) -> Optional[str]:
    if content is None:
        return None
    if not isinstance(content, str):
        content = str(content)
    text = content.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    if set(parsed) != {"route"}:
        return None
    route = parsed.get("route")
    if not isinstance(route, str):
        return None
    if route == _DEFAULT_ROUTE_NAME or route in _SUPPORTED_ROUTES:
        return route
    return None


def _route_from_config(config: dict, route_name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    routes = cfg_get(config, "kanban", "model_routing", "routes", default={})
    route_cfg = routes.get(route_name) if isinstance(routes, dict) else None
    if not isinstance(route_cfg, dict):
        return None, None, None
    provider = _normalize_text(route_cfg.get("provider"))
    model = _normalize_text(route_cfg.get("model"))
    try:
        reasoning_effort = normalize_reasoning_effort(route_cfg.get("reasoning_effort"))
    except (TypeError, ValueError):
        return None, None, None
    if not provider or not model or reasoning_effort is None:
        return None, None, None
    return provider, model, reasoning_effort


def _default_decision() -> KanbanModelRouteDecision:
    return KanbanModelRouteDecision(_DEFAULT_ROUTE_NAME, None, None, None, None)


def resolve_kanban_model_route(
    *,
    title: str,
    body: Optional[str] = None,
    explicit_model: Optional[str] = None,
    explicit_provider: Optional[str] = None,
    explicit_reasoning_effort: Optional[str] = None,
    config: Optional[dict] = None,
) -> KanbanModelRouteDecision:
    """Resolve the create-time route for one card.

    The resolver never mutates the database. It returns the explicit override
    unchanged when one was supplied, otherwise it consults the profile's
    `kanban.model_routing` config and falls back to the default profile route on
    any error, unsupported route, or classifier failure.
    """
    model = _normalize_text(explicit_model)
    provider = _normalize_text(explicit_provider)
    reasoning_effort = normalize_reasoning_effort(explicit_reasoning_effort)
    if provider and not model:
        raise ValueError("provider override requires a model override")
    if _route_is_explicit(model, provider, reasoning_effort):
        return KanbanModelRouteDecision("explicit", None, model, provider, reasoning_effort)

    config = config if isinstance(config, dict) else load_config_readonly()
    routing_cfg = cfg_get(config, "kanban", "model_routing", default={})
    if not isinstance(routing_cfg, dict) or not routing_cfg.get("enabled"):
        return _default_decision()

    classifier_cfg = routing_cfg.get("classifier") if isinstance(routing_cfg, dict) else None
    if not isinstance(classifier_cfg, dict):
        return _default_decision()
    classifier_provider = _normalize_text(classifier_cfg.get("provider"))
    classifier_model = _normalize_text(classifier_cfg.get("model"))
    if not classifier_provider or not classifier_model:
        return _default_decision()

    max_input_tokens = classifier_cfg.get("max_input_tokens", DEFAULT_CLASSIFIER_MAX_INPUT_TOKENS)
    try:
        max_input_tokens = int(max_input_tokens)
    except (TypeError, ValueError):
        max_input_tokens = DEFAULT_CLASSIFIER_MAX_INPUT_TOKENS
    max_input_tokens = max(256, min(max_input_tokens, 100_000))

    if _call_llm is None:
        return _default_decision()

    try:
        messages, _char_budget = _classifier_prompt(title, body, max_input_tokens=max_input_tokens)
        response = _call_llm(
            task="kanban_model_routing",
            provider=classifier_provider,
            model=classifier_model,
            messages=messages,
            temperature=0,
            max_tokens=32,
            extra_body={"response_format": {"type": "json_object"}},
        )
        content = response.choices[0].message.content
    except Exception as exc:
        logger.debug("Kanban model routing classifier failed closed: %s", exc)
        return _default_decision()

    route_name = _parse_classifier_route(content)
    if route_name not in _SUPPORTED_ROUTES:
        return _default_decision()

    route_provider, route_model, route_effort = _route_from_config(config, route_name)
    if not route_provider or not route_model or route_effort is None:
        return _default_decision()
    return KanbanModelRouteDecision(route_name, route_name, route_model, route_provider, route_effort)
