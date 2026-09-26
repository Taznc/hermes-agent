"""Kanban unattended model-routing / policy validation.

Extracted from ``hermes_cli.kanban_db`` (the fully-resolved
provider/model/reasoning-effort policy gate for unattended dispatch, plus the
per-profile route resolution it validates) behind one
``# >>> FORK ANCHOR: kanban-model-routing <<<`` import site. See
``hermes_fork/kanban/__init__.py`` for why ``hermes_fork/kanban/`` exists
despite docs/fork-anchor-extraction.md's earlier "do not create
hermes_fork/kanban/" verdict — this module is pure policy logic over a
``Task``/``sqlite3.Connection``, with no schema/migration or dashboard
ownership, the same shape as ``dispatch_resilience.py``.

Origin-resident helpers this module still needs (``normalize_reasoning_effort``,
``_canonical_assignee``, ``_board_meta_for``, ``get_task``, ``write_txn``,
``_task_status``, ``_append_event``, ``notify_task_updated``,
``_set_task_override``, and the ``Task`` type) are reached late-bound via
``_kb`` (import-cycle breaking, mirroring ``dispatch_resilience.py``'s own
``_kb``/``_kd`` pattern) so monkeypatching ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


@dataclass(frozen=True)
class ModelPolicyDecision:
    forced: bool
    force_reason: Optional[str] = None
    force_route: Optional[str] = None


_DEFAULT_UNATTENDED_ROUTES = frozenset({
    ("openai-codex", "gpt-5.6-luna", "low"),
    ("openai-codex", "gpt-5.6-terra", "medium"),
    ("openai-codex", "gpt-5.6-sol", "medium"),
    # The fleet's primary Claude routes and their successor models are
    # pre-approved at the profiles' configured effort tiers.
    ("anthropic", "claude-sonnet-5", "high"),
    ("anthropic", "claude-opus-5", "high"),
    ("anthropic", "claude-opus-5-5", "high"),
    ("openai-codex", "gpt-6-sol", "medium"),
})
_OPERATOR_ONLY_EFFORTS = frozenset({"high", "xhigh", "max", "ultra"})
_LUNA_INELIGIBLE_PROFILES = frozenset({"reviewer", "debugger"})


def _configured_policy_routes(policy: Optional[dict]) -> set[tuple[str, str, str]]:
    if not isinstance(policy, dict) or "allowed_routes" not in policy:
        return set(_DEFAULT_UNATTENDED_ROUTES)
    routes: set[tuple[str, str, str]] = set()
    raw_routes = policy.get("allowed_routes", [])
    if isinstance(raw_routes, list):
        for raw in raw_routes:
            if not isinstance(raw, dict):
                continue
            provider = str(raw.get("provider") or "").strip().casefold()
            model = str(raw.get("model") or "").strip().casefold()
            effort = str(raw.get("reasoning_effort") or "").strip().casefold()
            if provider and model and effort:
                routes.add((provider, model, effort))
    unsupported = routes - _DEFAULT_UNATTENDED_ROUTES
    if unsupported:
        rendered = ", ".join("/".join(route) for route in sorted(unsupported))
        raise ValueError(
            "kanban.model_policy.allowed_routes may only restrict the built-in unattended "
            f"allowlist; unsupported route(s): {rendered}"
        )
    return routes


def _force_route_json(
    *, provider: Optional[str], model: Optional[str], reasoning_effort: Optional[str],
    assignee: Optional[str],
) -> str:
    return json.dumps(
        {
            "assignee": assignee,
            "model": model,
            "provider": provider,
            "reasoning_effort": reasoning_effort,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_model_effort_policy(
    *, provider: Optional[str], model: Optional[str], reasoning_effort: Optional[str],
    assignee: Optional[str], policy: Optional[dict] = None, force: bool = False,
    force_reason: Optional[str] = None, forced_by: Optional[str] = None,
) -> ModelPolicyDecision:
    """Validate one fully-resolved unattended Kanban route.

    Unknown routes fail closed. Board/profile configuration may restrict the
    built-in triples through ``kanban.model_policy.allowed_routes``;
    it never guesses cost from a model name. Operator exceptions are bound to
    both the assignee and the exact route so a later handoff cannot inherit
    the approval silently.
    """
    provider = str(provider or "").strip().casefold() or None
    model = str(model or "").strip().casefold() or None
    effort = _kb.normalize_reasoning_effort(reasoning_effort)
    assignee = _kb._canonical_assignee(assignee) if assignee else None
    policy = policy if isinstance(policy, dict) else {}
    if policy.get("_fallback_routes"):
        raise ValueError(
            "Kanban model policy forbids hidden fallback routes; clear "
            f"fallback_providers/fallback_model for profile {assignee!r} before dispatch"
        )
    if provider == "moa" or (model and model.startswith("moa:")):
        raise ValueError(
            "Kanban model policy forbids MoA routes for unattended work; "
            "select one approved provider/model/effort triple"
        )
    reason = str(force_reason or "").strip() or None
    actor = _kb._canonical_assignee(forced_by) if forced_by else None
    if not force and (reason or actor):
        raise ValueError("model policy force reason/profile require policy_force=True")
    if not provider or not model or effort is None:
        raise ValueError(
            "Kanban model policy could not resolve provider/model/reasoning for the assignee; "
            "configure the profile route or set an approved explicit route"
        )
    categorically_denied = (
        "mini" in model
        or "spark" in model
        or model.endswith(":free")
        or model.endswith("/free")
    )
    if categorically_denied:
        raise ValueError(
            f"Kanban model policy denies {provider}/{model}/{effort}; "
            "mini, Spark, and free-tier models cannot be force-approved for unattended work"
        )
    allowed_models = {route[1] for route in _DEFAULT_UNATTENDED_ROUTES}
    canonical_efforts = {
        route_model: route_effort
        for _route_provider, route_model, route_effort in _DEFAULT_UNATTENDED_ROUTES
    }
    astra_model = model.startswith("gpt-") and model.endswith("-astra")
    forceable_model = provider == "openai-codex" and (
        (
            model in allowed_models
            and (effort == canonical_efforts[model] or effort in _OPERATOR_ONLY_EFFORTS)
        )
        or astra_model
    )
    if force:
        if not reason:
            raise ValueError("model policy force requires a non-empty reason")
        if not actor:
            raise ValueError("model policy force requires the operator profile that approved it")
        if not forceable_model:
            raise ValueError(
                f"Kanban model policy cannot force-approve unknown route {provider}/{model}/{effort}; "
                "operator exceptions are limited to Astra, approved models with operator-only "
                "effort, and profile-suitability overrides"
            )
        return ModelPolicyDecision(
            True, reason,
            _force_route_json(
                provider=provider, model=model, reasoning_effort=effort, assignee=assignee,
            ),
        )
    if "astra" in model or (
        effort in _OPERATOR_ONLY_EFFORTS
        and (provider, model, effort) not in _DEFAULT_UNATTENDED_ROUTES
    ):
        raise ValueError(
            f"Kanban model policy denies unattended route {provider}/{model}/{effort}; "
            "retry with operator force plus a durable non-empty reason"
        )
    if model == "gpt-5.6-luna" and assignee in _LUNA_INELIGIBLE_PROFILES:
        raise ValueError(
            f"Kanban model policy refuses mechanical-only Luna for {assignee!r}; "
            "use Sol/medium for independent review or hard debugging, or provide "
            "operator force plus a durable reason"
        )
    route = (provider, model, effort)
    if route not in _configured_policy_routes(policy):
        if model == "gpt-5.6-luna":
            detail = "Luna is allowed only with low reasoning effort"
        elif model in {"gpt-5.6-terra", "gpt-5.6-sol"}:
            detail = f"{model.rsplit('-', 1)[-1].title()} is allowed only with medium reasoning effort"
        else:
            detail = (
                "unknown model route (choose an approved exact route; "
                "policy configuration may restrict but cannot expand the built-in set)"
            )
        raise ValueError(f"Kanban model policy refuses {provider}/{model}/{effort}: {detail}")
    return ModelPolicyDecision(False)


def _validate_model_override(model: Optional[str], provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Strip both; a provider without a model is rejected (a bare ``--provider``
    would re-resolve the profile's model against another backend — exactly
    the mismatch the override exists to kill)."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    return model, provider


def _profile_route_and_policy(
    assignee: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str], dict]:
    if not assignee:
        return None, None, None, {}
    try:
        from hermes_cli.config import read_user_config_raw
        from hermes_cli.profiles import get_profile_dir

        path = get_profile_dir(assignee) / "config.yaml"
        raw = read_user_config_raw(path) if path.is_file() else {}
    except Exception:
        raw = {}
    model_cfg = raw.get("model", {}) if isinstance(raw, dict) else {}
    agent_cfg = raw.get("agent", {}) if isinstance(raw, dict) else {}
    kanban_cfg = raw.get("kanban", {}) if isinstance(raw, dict) else {}
    if isinstance(model_cfg, str):
        model, provider = model_cfg, None
    elif isinstance(model_cfg, dict):
        model = model_cfg.get("default") or model_cfg.get("model")
        provider = model_cfg.get("provider")
    else:
        model, provider = None, None
    effort = agent_cfg.get("reasoning_effort") if isinstance(agent_cfg, dict) else None
    policy = kanban_cfg.get("model_policy", {}) if isinstance(kanban_cfg, dict) else {}
    policy = dict(policy) if isinstance(policy, dict) else {}
    fallback_routes = (
        raw.get("fallback_providers") or raw.get("fallback_model")
        or (model_cfg.get("fallback_providers") if isinstance(model_cfg, dict) else None)
        or (model_cfg.get("fallback_model") if isinstance(model_cfg, dict) else None)
    )
    if fallback_routes:
        policy["_fallback_routes"] = fallback_routes
    return model, provider, effort, policy


def _profile_config_exists(assignee: Optional[str]) -> bool:
    if not assignee:
        return False
    try:
        from hermes_cli.profiles import get_profile_dir
        return (get_profile_dir(assignee) / "config.yaml").is_file()
    except Exception:
        return False


def _effective_model_policy(assignee: Optional[str], board: Optional[str]) -> dict:
    """Profile policy with board metadata layered over it.

    ``allowed_routes`` is restrictive: profile and board lists are intersected,
    and neither can expand the built-in unattended allowlist.
    """
    _model, _provider, _effort, profile_policy = _profile_route_and_policy(assignee)
    board_policy = _kb._board_meta_for(board).get("model_policy", {})
    if not isinstance(board_policy, dict):
        board_policy = {}
    merged = {**profile_policy, **board_policy}
    restrictions = [
        source.get("allowed_routes", [])
        for source in (profile_policy, board_policy)
        if "allowed_routes" in source
    ]
    if restrictions:
        route_maps = []
        for raw_routes in restrictions:
            mapping = {
                (
                    str(route.get("provider") or "").strip().casefold(),
                    str(route.get("model") or "").strip().casefold(),
                    str(route.get("reasoning_effort") or "").strip().casefold(),
                ): route
                for route in raw_routes if isinstance(route, dict)
            } if isinstance(raw_routes, list) else {}
            route_maps.append(mapping)
        allowed = set(route_maps[0])
        for mapping in route_maps[1:]:
            allowed &= set(mapping)
        merged["allowed_routes"] = [route_maps[0][route] for route in sorted(allowed)]
    else:
        merged.pop("allowed_routes", None)
    return merged


def resolved_task_model_route(
    task: "Task", *, board: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    profile_model, profile_provider, profile_effort, _policy = _profile_route_and_policy(task.assignee)
    return (
        task.provider_override or profile_provider,
        task.model_override or profile_model,
        task.reasoning_effort or profile_effort,
    )


def validate_task_model_policy(
    task: "Task", *, board: Optional[str] = None, allow_legacy_unconfigured: bool = False,
) -> ModelPolicyDecision:
    if task.goal_mode:
        raise ValueError("goal_mode is disabled by the unattended Kanban policy")
    provider, model, effort = resolved_task_model_route(task, board=board)
    if (
        allow_legacy_unconfigured
        and not _profile_config_exists(task.assignee)
        and not (
            task.model_override or task.provider_override or task.reasoning_effort is not None
            or task.policy_forced_by or task.policy_force_reason or task.policy_force_route
        )
    ):
        return ModelPolicyDecision(False)
    has_force = bool(task.policy_forced_by or task.policy_force_reason or task.policy_force_route)
    decision = validate_model_effort_policy(
        provider=provider,
        model=model,
        reasoning_effort=effort,
        assignee=task.assignee,
        policy=_effective_model_policy(task.assignee, board),
        force=has_force,
        force_reason=task.policy_force_reason,
        forced_by=task.policy_forced_by,
    )
    if has_force and decision.force_route != task.policy_force_route:
        raise ValueError(
            "stored model policy force does not match the current assignee/route; "
            "re-approve with force plus a durable reason"
        )
    return decision


def validate_review_task_model_policy(
    task: "Task", *, board: Optional[str] = None, allow_legacy_unconfigured: bool = False,
) -> ModelPolicyDecision:
    """Validate a task specifically for independent review execution.

    Review is never mechanical work, regardless of the reviewer's profile
    name. A same-profile operator-forced route remains an explicit exception;
    cross-profile handoffs clear force provenance before reaching this helper.
    """
    decision = validate_task_model_policy(
        task, board=board, allow_legacy_unconfigured=allow_legacy_unconfigured,
    )
    _provider, model, _effort = resolved_task_model_route(task, board=board)
    has_force = bool(task.policy_forced_by or task.policy_force_reason or task.policy_force_route)
    if str(model or "").strip().casefold() == "gpt-5.6-luna" and not has_force:
        raise ValueError(
            "Kanban model policy refuses mechanical-only Luna for review work; "
            "use Sol/medium or provide operator force plus a durable reason"
        )
    return decision


def set_reasoning_effort(
    conn: sqlite3.Connection, task_id: str, effort: Optional[str], *,
    policy_force: bool = False, policy_force_reason: Optional[str] = None,
    policy_forced_by: Optional[str] = None, board: Optional[str] = None,
) -> bool:
    """Set (empty clears; ``"none"`` pins thinking OFF) the per-task reasoning
    effort. Independent of the model override so clearing one never resets the
    other; applies on the NEXT dispatch, so settable while running."""
    effort = _kb.normalize_reasoning_effort(effort)
    task = _kb.get_task(conn, task_id)
    if task is None:
        return False
    candidate = replace(
        task, reasoning_effort=effort, policy_forced_by=None,
        policy_force_reason=None, policy_force_route=None,
    )
    resolved_provider, resolved_model, resolved_effort = resolved_task_model_route(candidate, board=board)
    decision = validate_model_effort_policy(
        provider=resolved_provider, model=resolved_model, reasoning_effort=resolved_effort,
        assignee=candidate.assignee, policy=_effective_model_policy(candidate.assignee, board),
        force=policy_force, force_reason=policy_force_reason, forced_by=policy_forced_by,
    ) if (
        policy_force or policy_force_reason or policy_forced_by
        or (
            (_profile_config_exists(candidate.assignee) or candidate.model_override
             or candidate.provider_override or candidate.reasoning_effort is not None)
            and resolved_provider and resolved_model and resolved_effort is not None
        )
    ) else ModelPolicyDecision(False)
    return _kb._set_task_override(
        conn, task_id,
        "UPDATE tasks SET reasoning_effort = ?, policy_forced_by = ?, "
        "policy_force_reason = ?, policy_force_route = ? WHERE id = ?",
        (effort, policy_forced_by if decision.forced else None,
         decision.force_reason, decision.force_route),
        "reasoning_effort_set", {
            "reasoning_effort": effort,
            "model_policy_force": ({"forced_by": policy_forced_by, "reason": decision.force_reason}
                                   if decision.forced else None),
        },
        ("reasoning_effort", "policy_forced_by", "policy_force_reason", "policy_force_route"),
        archived_msg="cannot set reasoning effort",
    )


def set_route_overrides(
    conn: sqlite3.Connection, task_id: str, *, model: Optional[str],
    provider: Optional[str], reasoning_effort: Optional[str],
    policy_force: bool = False, policy_force_reason: Optional[str] = None,
    policy_forced_by: Optional[str] = None, board: Optional[str] = None,
) -> bool:
    """Atomically set and validate a complete model/provider/effort change."""
    model, provider = _validate_model_override(model, provider)
    effort = _kb.normalize_reasoning_effort(reasoning_effort)
    task = _kb.get_task(conn, task_id)
    if task is None:
        return False
    candidate = replace(
        task, model_override=model, provider_override=provider, reasoning_effort=effort,
        policy_forced_by=None, policy_force_reason=None, policy_force_route=None,
    )
    resolved_provider, resolved_model, resolved_effort = resolved_task_model_route(
        candidate, board=board,
    )
    decision = validate_model_effort_policy(
        provider=resolved_provider, model=resolved_model, reasoning_effort=resolved_effort,
        assignee=candidate.assignee, policy=_effective_model_policy(candidate.assignee, board),
        force=policy_force, force_reason=policy_force_reason, forced_by=policy_forced_by,
    ) if (
        policy_force or policy_force_reason or policy_forced_by
        or (
            (_profile_config_exists(candidate.assignee) or candidate.model_override
             or candidate.provider_override or candidate.reasoning_effort is not None)
            and resolved_provider and resolved_model and resolved_effort is not None
        )
    ) else ModelPolicyDecision(False)
    with _kb.write_txn(conn):
        status = _kb._task_status(conn, task_id)
        if status is None:
            return False
        if status == "archived":
            raise RuntimeError(f"cannot set route overrides on archived task {task_id}")
        conn.execute(
            "UPDATE tasks SET model_override = ?, provider_override = ?, reasoning_effort = ?, "
            "policy_forced_by = ?, policy_force_reason = ?, policy_force_route = ? WHERE id = ?",
            (model, provider, effort, policy_forced_by if decision.forced else None,
             decision.force_reason, decision.force_route, task_id),
        )
        force_payload = ({"forced_by": policy_forced_by, "reason": decision.force_reason}
                         if decision.forced else None)
        _kb._append_event(
            conn, task_id, "model_override_set",
            {"model": model, "provider": provider, "model_policy_force": force_payload},
        )
        _kb._append_event(
            conn, task_id, "reasoning_effort_set",
            {"reasoning_effort": effort, "model_policy_force": force_payload},
        )
    _kb.notify_task_updated(
        conn, task_id,
        ("model_override", "provider_override", "reasoning_effort", "policy_forced_by",
         "policy_force_reason", "policy_force_route"),
    )
    return True


from hermes_cli import kanban_db as _kb  # noqa: E402
