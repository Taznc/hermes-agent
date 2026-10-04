"""Unattended Kanban model-route policy (fork-owned, T1).

The dispatcher launches workers with nobody watching, so the model route a
worker will run on is an operator decision, not something a card or a profile
edit can widen silently. This module is the admission check the dispatcher
runs synchronously after it claims a card and before it resolves a workspace
or spawns anything (anchor ``kanban-unattended-route-policy`` in
``hermes_cli/kanban_db_dispatch.py::_dispatch_lane_task``).

What is checked, per card:

* the PRIMARY route the worker will start on: the task's ``model_override`` /
  ``provider_override`` / ``reasoning_effort`` when set, otherwise the assignee
  profile's own ``model.default`` / ``model.provider`` and its effective
  reasoning effort (``agent.reasoning_overrides`` for that model, then
  ``agent.reasoning_effort`` — the same ``resolve_reasoning_config`` the worker
  CLI uses);
* EVERY fallback route the worker could switch to (``fallback_providers`` /
  ``fallback_model``, root and ``model.``-nested), each at the effort the
  fallback activation re-resolves from config (the task's ``--reasoning`` does
  not survive a fallback swap).

Each ``(provider, model, effort)`` triple must be an exact member of
:data:`APPROVED_UNATTENDED_ROUTES`. Anything unresolved (no model, provider
``auto``/empty, no effort) fails closed. Nous Portal routes and free / mini /
Spark model markers are denied categorically, BEFORE the allowlist, so adding
one to the allowlist can never admit it: a free-tier Nous account cannot be
detected synchronously (``hermes_cli.models.check_nous_free_tier`` fails open
to "paid" on unknown), so admission refuses the provider outright.

A denial blocks the card ``needs_input`` with the offending route in the
reason (sticky: ``recompute_ready`` never auto-resumes it; an unblock that
hits the same denial again escalates to triage via the block-loop breaker).

This is NOT the create-time ``kanban.model_routing`` classifier from ``dev``
(it picks a route when a card is created); that stays unported — it is
disabled in the live config.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

_log = logging.getLogger(__name__)

#: Exact ``(provider, model, reasoning_effort)`` triples approved for
#: unattended Kanban workers. Ported from ``dev``'s
#: ``hermes_fork/kanban/model_routing.py`` ``_DEFAULT_UNATTENDED_ROUTES``.
#: Widening this set is an operator decision; keep it in code review.
APPROVED_UNATTENDED_ROUTES: frozenset[tuple[str, str, str]] = frozenset({
    ("anthropic", "claude-opus-5-5", "high"),
    ("anthropic", "claude-opus-5", "high"),
    ("anthropic", "claude-sonnet-5-5", "high"),
    ("anthropic", "claude-sonnet-5", "high"),
    ("openai-codex", "gpt-6.1-sol", "medium"),
    ("openai-codex", "gpt-6-sol", "medium"),
    ("openai-codex", "gpt-5.6-sol", "medium"),
    ("openai-codex", "gpt-5.6-terra", "medium"),
    ("openai-codex", "gpt-5.6-luna", "low"),
})

#: Provider ids (and aliases) for Nous Portal; see module docstring.
_NOUS_PROVIDERS = frozenset({"nous", "nous-portal", "nousresearch"})
#: Whole-token model markers that are never admitted unattended.
_DENIED_MODEL_TOKENS = frozenset({"free", "mini", "spark"})
_TOKEN_SPLIT = re.compile(r"[\s/:._-]+")
#: Keys that redirect a route to another endpoint or credential without
#: changing its (provider, model) identity: ``resolve_runtime_with_fallback``
#: forwards a fallback entry's ``base_url`` / key into ``resolve_runtime_provider``,
#: and ``model.base_url`` is honoured for the configured provider (e.g.
#: ``_anthropic_cfg_base_url``). The allowlist approves an identity, not an
#: endpoint, so any non-empty override fails closed.
_ENDPOINT_OVERRIDE_KEYS = ("base_url", "api_key", "key_env", "api_key_env")

#: Test-suite seam; no production code sets it. Upstream's own dispatch suites
#: create cards for throwaway, model-less profiles and test claim/spawn
#: mechanics, not route policy, so the repo-root ``conftest.py`` sets this for
#: every test OUTSIDE ``tests/hermes_fork/`` (where the policy is exercised
#: with the gate live). Only in-process code can flip it: there is no config
#: key or environment variable behind it.
_ADMIT_ALL_FOR_TESTS = False


@dataclass(frozen=True)
class Route:
    """One fully resolved route a worker could run on."""

    source: str  # "primary" | "fallback[<i>]"
    provider: str
    model: str
    effort: Optional[str]
    #: Non-empty endpoint/credential override keys carried by this route.
    endpoint_overrides: tuple[str, ...] = ()

    def render(self) -> str:
        return f"{self.source} {self.provider or '?'}/{self.model or '?'}/{self.effort or '?'}"


def _clean(value: Any) -> str:
    return str(value).strip().casefold() if isinstance(value, str) else ""


def _endpoint_overrides(entry: Any) -> tuple[str, ...]:
    if not isinstance(entry, dict):
        return ()
    return tuple(k for k in _ENDPOINT_OVERRIDE_KEYS if str(entry.get(k) or "").strip())


def _effort_label(reasoning: Optional[dict]) -> Optional[str]:
    """``resolve_reasoning_config`` / ``parse_reasoning_effort`` dict -> level string."""
    if not isinstance(reasoning, dict):
        return None
    if reasoning.get("enabled") is False:
        return "none"
    return _clean(reasoning.get("effort")) or None


def _config_effort(config: dict, model: str) -> Optional[str]:
    from hermes_constants import resolve_reasoning_config

    return _effort_label(resolve_reasoning_config(config, model))


def _fallback_entries(config: dict) -> list[dict]:
    """Every fallback entry the worker could activate, root and ``model.``-nested."""
    from hermes_cli.fallback_config import get_fallback_chain

    entries = list(get_fallback_chain(config))
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        entries += get_fallback_chain(model_cfg)
    return entries


def resolve_routes(task: "Task", config: dict) -> list[Route]:
    """Primary route plus every fallback route for *task* under the assignee's *config*."""
    config = config if isinstance(config, dict) else {}
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        cfg_model = model_cfg.get("default") or model_cfg.get("model")
        cfg_provider = model_cfg.get("provider")
    else:
        cfg_model, cfg_provider = model_cfg, None

    override = (task.model_override or "").strip()
    if override:
        # The worker gets ``-m <override>`` and ``--provider`` only when the
        # card pins one; otherwise the CLI resolves it against the profile's
        # configured provider.
        model = override
        provider = (task.provider_override or "").strip() or cfg_provider
    else:
        model, provider = cfg_model, cfg_provider
    model_s = str(model).strip() if isinstance(model, str) else ""

    if task.reasoning_effort:
        from hermes_constants import parse_reasoning_effort

        effort = _effort_label(parse_reasoning_effort(task.reasoning_effort))
    else:
        effort = _config_effort(config, model_s)

    # ``model.base_url`` / ``model.api_key`` are judged on the primary whatever
    # its provider: whether the resolver applies them depends on provider
    # matching rules this gate does not re-implement, so presence fails closed.
    routes = [Route("primary", _clean(provider), _clean(model_s), effort,
                    tuple(f"model.{k}" for k in _endpoint_overrides(model_cfg)))]
    for i, entry in enumerate(_fallback_entries(config)):
        fb_model = str(entry.get("model") or "").strip()
        routes.append(Route(
            f"fallback[{i}]", _clean(entry.get("provider")), _clean(fb_model),
            _config_effort(config, fb_model), _endpoint_overrides(entry),
        ))
    return routes


def route_denial(route: Route) -> Optional[str]:
    """Why *route* may not run unattended, or ``None`` when it is approved."""
    if not route.model or not route.provider or route.provider == "auto" or not route.effort:
        return f"{route.render()} is not fully resolved (model, explicit provider and reasoning effort are all required)"
    if route.provider in _NOUS_PROVIDERS:
        return (f"{route.render()} is denied: Nous Portal routes never run unattended "
                "(free-tier status cannot be verified at admission)")
    tokens = set(_TOKEN_SPLIT.split(route.model))
    marker = sorted(tokens & _DENIED_MODEL_TOKENS)
    if marker:
        return f"{route.render()} is denied: {marker[0]!r} models never run unattended"
    if (route.provider, route.model, route.effort) not in APPROVED_UNATTENDED_ROUTES:
        return f"{route.render()} is not an approved unattended route"
    if route.endpoint_overrides:
        return (f"{route.render()} is denied: it carries endpoint/credential overrides "
                f"({', '.join(route.endpoint_overrides)}); approved routes run only on the provider's own endpoint")
    return None


def load_assignee_config(assignee: str) -> dict:
    """The assignee profile's effective user config (no defaults), read fail-closed."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.profiles import get_profile_dir

    return load_user_config_effective(get_profile_dir(assignee) / "config.yaml", fail_closed=True)


def unattended_route_denial(task: "Task", config: Optional[dict] = None) -> Optional[str]:
    """First denial across every route *task* could run on, or ``None`` when all are approved."""
    if config is None:
        if not task.assignee:
            return "card has no assignee profile to resolve a route from"
        config = load_assignee_config(task.assignee)
    for route in resolve_routes(task, config):
        reason = route_denial(route)
        if reason:
            return reason
    return None


def deny_unapproved_route(conn: sqlite3.Connection, task: "Task", result: Any, *, board: Optional[str] = None) -> bool:
    """Dispatcher admission gate for a freshly CLAIMED card. True = do not spawn.

    On denial the card is blocked ``needs_input`` (its run closed, the claim
    released) and its id lands in ``result.auto_blocked``. Any error while
    resolving the route is itself a denial: the gate fails closed.

    Runs for every spawner, including a ``spawn_fn`` injected into
    ``dispatch_once``: an injected spawner still launches an unattended worker.
    """
    if _ADMIT_ALL_FOR_TESTS:
        return False
    try:
        reason = unattended_route_denial(task)
    except Exception as exc:  # unreadable config, bad profile name, ...
        reason = f"route could not be resolved for profile {task.assignee!r}: {type(exc).__name__}: {exc}"
    if reason is None:
        return False
    from hermes_cli import kanban_db as kb

    message = f"kanban unattended route policy: {reason}"
    if not kb.block_task(conn, task.id, reason=message, kind="needs_input"):
        # Lost a race with another transition; still never spawn on a denied route.
        _log.warning("kanban route policy: could not block %s (board=%s): %s", task.id, board, message)
    _log.warning("kanban route policy blocked %s (board=%s): %s", task.id, board, reason)
    result.auto_blocked.append(task.id)
    return True
