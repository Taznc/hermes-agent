"""Rework-items preflight for ``kanban_request_review``.

A rework run — one dispatched after a reviewer's ``changes_requested`` — that
comes back missing one or more of the reviewer's numbered items (the
implementer timed out, or stopped early) burns the card's next review round on
"items 2 and 3 still not done". With ``kanban.max_review_rounds`` at 2 that is
the main way a card hits the round cap. The reviewer cannot tell from a prose
summary whether every item was addressed, so the handoff has to say so
mechanically: ``metadata.rework_items`` maps EACH item from the latest
``changes_requested`` reason to the commit/test/output that proves it.

This is the second check on the review-lane preflight path, next to the
mergeability preflight in :mod:`tools.kanban_tools_mergeability`. It shares
that module's shape — one helper, both doors (``kanban_request_review`` and
``hermes kanban request-review``) call it, status gated first, operator off
switch in config — and none of its git; this check is a single DB read against
the task's events. Prior rounds are counted exactly the way the dispatcher's
round cap counts them (``kanban_db_dispatch._changes_requested_state``), so
"one prior round" here and "one round toward the cap" there are the same
number.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from hermes_cli.config import cfg_get, load_config
from tools.kanban_tools_mergeability import _REVIEWABLE_STATUSES

logger = logging.getLogger(__name__)

# The reviewer's reason is quoted back so the implementer needs no second
# lookup; bounded so a long review does not blow up the refusal.
_REASON_QUOTE_CHARS = 600


def rework_items_problem(value: Any) -> Optional[str]:
    """Why ``value`` is not a valid ``rework_items`` list, or ``None`` if it is.

    Valid: a non-empty list whose every element is a dict with non-empty
    string ``item`` and ``evidence``. The message names the first offending
    element so the fix is one edit, not a guess.
    """
    if value is None:
        return "metadata.rework_items is missing"
    if not isinstance(value, list):
        return f"metadata.rework_items must be a list, got {type(value).__name__}"
    if not value:
        return "metadata.rework_items is empty"
    for i, entry in enumerate(value, 1):
        if not isinstance(entry, dict):
            return (f"metadata.rework_items[{i}] must be an object with 'item' and "
                    f"'evidence', got {type(entry).__name__}")
        for key in ("item", "evidence"):
            got = entry.get(key)
            if not isinstance(got, str) or not got.strip():
                return f"metadata.rework_items[{i}] needs a non-empty string {key!r}"
    return None


def refusal_message(*, rounds: int, problem: str, reason: Optional[str],
                    task_status: str) -> str:
    """The text the implementer reads: what is missing, the exact shape to
    add, and the reviewer's reason it must be mapped against."""
    if reason:
        quoted = reason.strip()
        if len(quoted) > _REASON_QUOTE_CHARS:
            quoted = quoted[:_REASON_QUOTE_CHARS].rstrip() + " […]"
        reason_block = "\n".join(f"  {line}" for line in quoted.splitlines()) or "  (blank)"
    else:
        reason_block = "  (the changes_requested event recorded no reason text)"
    plural = "" if rounds == 1 else "s"
    return (
        f"kanban_request_review refused: this task has {rounds} prior changes_requested "
        f"round{plural} since its last completion, and {problem}. A rework handoff must "
        f"list metadata.rework_items=[{{item, evidence}}] mapping EACH numbered item from "
        f"the reviewer's latest changes_requested reason to the commit/test/output that "
        f"proves it, so the reviewer does not spend a round rediscovering what was "
        f"skipped.\n\n"
        f"Add to your request, one entry per reviewer item:\n"
        f"  metadata.rework_items = [\n"
        f"    {{\"item\": \"<reviewer item 1, in their words>\", "
        f"\"evidence\": \"<commit sha / test command + result / output path>\"}},\n"
        f"    ...\n"
        f"  ]\n\n"
        f"Reviewer's latest changes_requested reason:\n{reason_block}\n\n"
        f"Your task is unchanged and still {task_status}. Requests with zero prior "
        f"rounds are not gated; the operator off switch is "
        f"`kanban.require_rework_items_for_review: false`.\n"
    )


def preflight(conn, task, task_id: str, metadata: Optional[dict]) -> Optional[str]:
    """Refusal text when a rework handoff lacks valid ``rework_items``; ``None``
    to let the handoff proceed.

    Status is checked first for the same reason the mergeability preflight
    does it: a card that cannot enter the review lane at all must get
    ``kb.request_review()``'s status answer, not an unrelated one about
    rework items. A first-time request (zero ``changes_requested`` events
    since the latest ``completed``) is never gated.
    """
    if task is None or getattr(task, "status", None) not in _REVIEWABLE_STATUSES:
        logger.debug("rework-items preflight skipped for %s: status %r cannot enter review",
                     task_id, getattr(task, "status", None))
        return None
    if not cfg_get(load_config(), "kanban", "require_rework_items_for_review", default=True):
        return None
    # Lazy like ``kanban_tools._board``: the tool module must load in
    # non-kanban contexts without pulling in the DB layer.
    from hermes_cli import kanban_db_dispatch as kbd

    rounds, _latest_id = kbd._changes_requested_state(conn, task_id)
    if rounds < 1:
        return None
    problem = rework_items_problem((metadata or {}).get("rework_items"))
    if problem is None:
        return None
    return refusal_message(
        rounds=rounds, problem=problem,
        reason=kbd._last_changes_requested_reason(conn, task_id),
        task_status=task.status,
    )
