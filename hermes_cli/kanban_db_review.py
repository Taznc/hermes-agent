"""Deterministic review-contract policy shared by Kanban surfaces.

The facade owns lifecycle writes; this sibling owns the small pure/SQL policy
that validates blocker scope and describes the effective same-card or ready-child
review contract.  No classifier or inference call participates in a verdict.
"""

from __future__ import annotations

from typing import Any, Optional


VALID_REVIEW_BASES = (
    "original_ac",
    "required_behavior",
    "base_regression",
    "landing_gate",
)
_VALID_REVIEW_BASES = frozenset(VALID_REVIEW_BASES)


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalize_blockers(blockers: Any) -> list[dict[str, str]]:
    """Validate and normalize one consolidated blocking verdict."""
    if not isinstance(blockers, list) or not blockers:
        raise ValueError("at least one structured blocker is required")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(blockers, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"blocker {index} must be an object")
        basis = _text(raw.get("basis"))
        reference = _text(raw.get("reference"))
        rework_of = _text(raw.get("rework_of"))
        if basis not in _VALID_REVIEW_BASES:
            raise ValueError(
                f"blocker {index} basis must be one of {', '.join(VALID_REVIEW_BASES)}"
            )
        if reference is None:
            raise ValueError(f"blocker {index} reference is required")
        if reference in seen:
            raise ValueError(f"duplicate blocker reference: {reference}")
        seen.add(reference)
        item = {"basis": basis, "reference": reference}
        if rework_of is not None:
            item["rework_of"] = rework_of
        normalized.append(item)
    return normalized


def normalize_followups(followups: Any) -> list[str]:
    """Validate inert, non-blocking review suggestions."""
    if followups is None:
        return []
    if not isinstance(followups, list):
        raise ValueError("followups must be a list of non-empty strings")
    normalized: list[str] = []
    for index, raw in enumerate(followups, start=1):
        value = _text(raw)
        if value is None:
            raise ValueError(f"followup {index} must be a non-empty string")
        normalized.append(value)
    return normalized


def _changes_events_since_completion(conn, task_id: str):
    return conn.execute(
        "SELECT id, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'changes_requested' "
        "AND id > COALESCE((SELECT MAX(id) FROM task_events "
        "WHERE task_id = ? AND kind = 'completed'), 0) ORDER BY id",
        (task_id, task_id),
    ).fetchall()


def cited_references(conn, task_id: str) -> set[str]:
    """References established by the first structured verdict in this cycle.

    Legacy prose-only events are skipped.  The first post-upgrade structured
    verdict therefore seeds a contract without rewriting historical rows.
    """
    from hermes_cli import kanban_db as kb

    for row in _changes_events_since_completion(conn, task_id):
        payload = kb._json_dict(row["payload"])
        try:
            blockers = normalize_blockers(payload.get("blockers"))
        except ValueError:
            continue
        return {item["reference"] for item in blockers}
    return set()


def validate_verdict(
    conn,
    task_id: str,
    *,
    blockers: Any,
    followups: Any = None,
) -> tuple[list[dict[str, str]], list[str], bool]:
    """Return normalized verdict fields and whether this seeded legacy state."""
    normalized = normalize_blockers(blockers)
    inert_followups = normalize_followups(followups)
    prior_events = _changes_events_since_completion(conn, task_id)
    references = cited_references(conn, task_id)
    seeded_from_legacy = bool(prior_events) and not references
    if prior_events and references:
        for blocker in normalized:
            reference = blocker["reference"]
            if reference in references:
                continue
            if (
                blocker["basis"] == "base_regression"
                and blocker.get("rework_of") in references
            ):
                continue
            raise ValueError(
                f"blocker reference {reference!r} is outside the first-round review contract; "
                "re-review may cite an established blocker or a base_regression whose "
                "rework_of names an established blocker"
            )
    return normalized, inert_followups, seeded_from_legacy


def configured_max_review_rounds() -> int:
    """Resolve the exact persisted/prompted round cap from current config."""
    from hermes_cli.config import load_config
    from hermes_cli import kanban_db_dispatch as dispatch

    try:
        raw = load_config()
        kanban = raw.get("kanban", {}) if isinstance(raw, dict) else {}
    except Exception:
        kanban = {}
    return dispatch.resolve_dispatch_caps(
        kanban if isinstance(kanban, dict) else {}
    ).max_review_rounds


def is_ready_review_child(
    conn,
    task_id: str,
    task: Any,
    *,
    source_state: Optional[str] = None,
    exclude_parent_id: Optional[str] = None,
) -> bool:
    """True for a forced ready-origin review with a durable parent deliverable."""
    state = source_state or getattr(task, "status", None)
    if state != "ready" or "sdlc-review" not in set(
        getattr(task, "skills", None) or ()
    ):
        return False
    sql = "SELECT 1 FROM task_links WHERE child_id = ?"
    params: tuple[str, ...] = (task_id,)
    if exclude_parent_id is not None:
        sql += " AND parent_id != ?"
        params += (exclude_parent_id,)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def effective_review_contract(
    conn,
    task_id: str,
    *,
    task,
    source_state: str,
    changes_rounds: int,
    max_review_rounds: int,
) -> dict[str, Any]:
    """Describe one review contract for same-card and explicit ready-child flows."""
    ready_child = is_ready_review_child(
        conn, task_id, task, source_state=source_state
    )
    path = "ready_child" if ready_child else "same_card"
    references = sorted(cited_references(conn, task_id))
    target_task_ids = _kb.parent_ids(conn, task_id) if ready_child else [task_id]
    current_round = (
        changes_rounds + 1
        if ready_child
        else changes_rounds + (1 if source_state == "review" else 0)
    )
    return {
        "path": path,
        "target_task_ids": target_task_ids,
        "current_round": current_round,
        "changes_requested_rounds": changes_rounds,
        "max_rounds": max_review_rounds,
        "cited_references": references,
        "contract": {
            "allowed_bases": list(VALID_REVIEW_BASES),
            "first_round": "consolidate_all_blockers",
            "rereview": "cited_blockers_and_rework_regressions_only",
            "followups": "inert_non_blocking",
        },
    }


from hermes_cli import kanban_db as _kb  # noqa: E402
