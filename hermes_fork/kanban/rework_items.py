"""Bounded rework receipt gate, composed through the existing pre-review seam.

Port of dev dbe0bcaa03/e6d720ea20/c03d49dcf1: nonempty item/evidence strings,
entry count >= line-leading numbered reviewer items, full rejection quote.
This is NOT semantic evidence verification: duplicates/unrelated entries pass.
Free-form reasons get shape/presence only. First reviews are exempt; routing's
loop state preserves approval resets and ignores returned diagnosis completions.
Unlike dev, opt in with ``kanban.require_rework_items_for_review: true`` to
preserve next's ungated default. ``force=True`` bypasses the shared seam.
"""
from __future__ import annotations

import re
from typing import Any

_NUMBERED = re.compile(r"(?m)^[ \t]{0,3}(\d+)[.)][ \t]+\S")


def enumerated_item_count(reason: str) -> int:
    """Count numbered lines, not the largest index or mid-sentence numbers."""
    return len(_NUMBERED.findall(reason))


def rework_items_problem(value: Any, *, min_count: int = 0) -> str | None:
    if not isinstance(value, list) or not value:
        return "metadata.rework_items must be a non-empty list"
    for i, entry in enumerate(value, 1):
        if not isinstance(entry, dict):
            return f"metadata.rework_items[{i}] must be an object with item and evidence"
        for key in ("item", "evidence"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                return f"metadata.rework_items[{i}] needs a non-empty string {key!r}"
    if len(value) < min_count:
        return (f"metadata.rework_items has {len(value)} entries but the reviewer's "
                f"reason enumerates {min_count} items")
    return None


def refusal(conn, task_id: str, metadata: dict | None) -> str | None:
    from hermes_cli.config import cfg_get, load_config_readonly
    from hermes_fork.kanban.review_gate import _TRUE_STRINGS
    from hermes_fork.kanban.review_routing import _loop_state, _payload

    value = cfg_get(load_config_readonly(), "kanban", "require_rework_items_for_review", default=False)
    enabled = value.strip().lower() in _TRUE_STRINGS if isinstance(value, str) else value is True
    if not enabled:
        return None
    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["status"] not in ("ready", "running"):
        return None  # let upstream return its task/status error
    rounds, latest, _ = _loop_state(conn, task_id)
    if not rounds:
        return None
    event = conn.execute("SELECT payload FROM task_events WHERE id = ?", (latest,)).fetchone()
    reason = _payload(event["payload"]).get("reason")
    reason = reason if isinstance(reason, str) else ""
    items = metadata.get("rework_items") if isinstance(metadata, dict) else None
    problem = rework_items_problem(items, min_count=enumerated_item_count(reason))
    if problem is None:
        return None
    quote = "\n".join(f"  {line}" for line in reason.splitlines()) or "  (no reason recorded)"
    return (
        f"review handoff refused: {rounds} prior changes_requested round(s) in this "
        f"review loop; {problem}. Add metadata.rework_items = "
        '[{"item": "<reviewer item>", "evidence": "<commit/test command + result/output>"}] '
        "with at least one entry per numbered reviewer item. Free-form reasons require "
        "at least one entry. This is a count-bound shape gate, not semantic verification; "
        "the reviewer must confirm correspondence and evidence.\n"
        f"Reviewer's latest changes_requested reason:\n{quote}\n"
        f"Your task is unchanged and still {task['status']}. Operator off switch: "
        "kanban.require_rework_items_for_review: false."
    )
