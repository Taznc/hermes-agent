"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the gateway
dispatcher's auto-decompose path. Reads the profile roster (with
descriptions), asks the auxiliary LLM for a task graph in JSON, then
atomically creates the children, links them under the root, and flips the
root ``triage -> todo``. The root stays alive as parent of every leaf child so
it wakes back up when the graph completes and its assignee (the orchestrator
profile) can judge completion and add more work.

Mirrors ``kanban_specify`` (lazy aux import, lenient parse, never raises on
expected failures). ``fanout=false`` collapses to the ``specify`` behaviour
(tighten + promote, no children), making ``decompose`` a strict superset.
Unknown assignees are rewritten to ``default_assignee`` — a child NEVER ends
up with ``assignee=None``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import profiles as profiles_mod
from hermes_cli.kanban_specify import (
    _call_aux, _extract_json_blob, _load_triage_task, _task_prompt_fields, _title_body,
)
from hermes_cli.kanban_specify import _profile_author as _specify_author

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task. Scope each implementation child to one
    coherent behavior or ownership boundary that a worker can usually finish
    in 1-4 hours; do not split mechanical steps into separate cards.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context. It MUST follow this contract exactly — a body that breaks it is
    rejected and you are asked to redo the whole answer:
      1. AT MOST 5 acceptance criteria, numbered "AC1".."AC5", each ONE
         observable, testable sentence. If the work needs more than 5, that is
         the signal to SPLIT it into more children joined by real "parents"
         edges — never exceed 5 in one child.
      2. Any criterion using a quantifier ("every", "all", "any malformed")
         must ENUMERATE the finite set inline, e.g. "every malformed input:
         empty string, non-JSON, JSON without a `route` key, `route` not in
         {default, mechanical}". An open-ended quantifier is what makes a
         reviewer expand one new sub-case per round.
      3. A "## Out of scope" section naming at least one explicit non-goal, so
         the reviewer's scope-lock has something to lock against.
      4. A named test obligation per criterion:
         "Tests: <test file>::<test name or behavior>" — never "add
         appropriate coverage".
      5. Aim for 2,500 characters or fewer; 4,000 is the hard ceiling. A larger
         review surface produces more findings and more rounds.
    Also include: in-scope behavior, constraints/decisions, an "Edit-Targets:"
    line, and the evidence expected in the handoff.
  - Never give parallel children overlapping Edit-Targets. If two children
    must modify the same file, merge them into one coherent child or add a real
    parent dependency so the edits are serialized.
  - If the original task already records an upstream/prior-art search result,
    pass that result into relevant children. Do not ask every child to repeat
    the same discovery search.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_RETRY_TEMPLATE = """
YOUR PREVIOUS ANSWER WAS REJECTED: {violation}

Re-emit the whole JSON object with that fixed. Every child body must have at
most {max_acs} numbered acceptance criteria (AC1..AC{max_acs}) and a
"## Out of scope" section. If the work needs more criteria than that, split it
into MORE children joined by real "parents" edges — do not exceed the cap.
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Child-body contract. A review round costs a full reviewer session plus a full
# implementer session, and `kanban.max_review_rounds` is 2 — a card carrying an
# open-ended review surface blocks instead of converging. Two of the five prompt
# rules are mechanically checkable, so they are ENFORCED here rather than merely
# asked for: an over-specified card is the single largest driver of extra rounds,
# and an absent "Out of scope" section leaves the reviewer's scope-lock with
# nothing to lock against. The other three (quantifier enumeration, per-AC test
# names, body length) are judgment calls that only the prompt can carry.
_MAX_ACS_PER_CHILD = 5
_AC_LABEL_RE = re.compile(r"\bAC(\d+)\b")
_OUT_OF_SCOPE_RE = re.compile(r"^#{1,6}\s*out[ -]of[ -]scope\b", re.IGNORECASE | re.MULTILINE)


def _child_body_violation(body: str) -> str:
    """``""`` when ``body`` satisfies the child-body contract, else a one-line
    reason naming the violation (fed back to the LLM verbatim on the retry)."""
    text = body or ""
    labels = set(_AC_LABEL_RE.findall(text))
    if len(labels) > _MAX_ACS_PER_CHILD:
        return (
            f"has {len(labels)} acceptance criteria (AC labels: "
            f"{', '.join('AC' + n for n in sorted(labels, key=int))}); "
            f"the cap is {_MAX_ACS_PER_CHILD} — split the work into more children instead"
        )
    if not _OUT_OF_SCOPE_RE.search(text):
        return "is missing the mandatory '## Out of scope' section"
    return ""


def _contract_violation(parsed: dict) -> str:
    """First child-body contract violation in a ``fanout=true`` reply, prefixed
    with which child broke it; ``""`` when every child conforms."""
    raw_tasks = parsed.get("tasks")
    if not isinstance(raw_tasks, list):
        return ""
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        violation = _child_body_violation(body if isinstance(body, str) else "")
        if violation:
            title = entry.get("title")
            label = title.strip() if isinstance(title, str) and title.strip() else "(untitled)"
            return f"child {idx} {label!r} {violation}"
    return ""


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return _specify_author("decomposer")


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_profile_from_cfg(cfg: dict, key: str) -> str:
    """``kanban.<key>`` if it names an existing profile, else the active
    default profile — so a task is never stranded for lack of an owner.
    ``orchestrator_profile`` owns the root after fan-out; ``default_assignee``
    catches children the decomposer can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get(key) or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """``(roster_for_prompt, valid_assignee_names)``; entries are
    ``{name, description, has_description}``."""
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return [], set()
    roster = []
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
    return roster, {p.name for p in all_profiles}


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    return "\n".join(
        f"  - {entry['name']}{'' if entry['has_description'] else ' ⚠ undescribed'}: {entry['description']}"
        for entry in roster
    )


def _normalize_assignee_choice(assignee: object, *, default_assignee: str, valid_names: set[str]) -> str:
    """A valid assignee, else ``default_assignee`` — promoted work is never
    left unassigned."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    return chosen if chosen in valid_names else default_assignee


@dataclass
class _Routing:
    """Config-derived routing context for one decomposition."""

    orchestrator: str
    default_assignee: str
    auto_promote: bool
    roster: list[dict]
    valid_names: set[str]


def _load_routing() -> _Routing:
    cfg = _load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    roster, valid_names = _build_roster()
    return _Routing(
        orchestrator=_resolve_profile_from_cfg(cfg, "orchestrator_profile"),
        default_assignee=_resolve_profile_from_cfg(cfg, "default_assignee"),
        auto_promote=bool(kanban_cfg.get("auto_promote_children", True)),
        roster=roster,
        valid_names=valid_names,
    )


def _apply_single(task: kb.Task, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    """``fanout=false``: single-task spec promotion (same effect as specify)."""
    title_val, body_val = _title_body(parsed)
    assignee_val = None
    if not task.assignee:
        assignee_val = _normalize_assignee_choice(
            parsed.get("assignee"), default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
    if title_val is None and body_val is None:
        return DecomposeOutcome(task.id, False, "decomposer returned fanout=false with no title/body")
    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn, task.id, title=title_val, body=body_val, assignee=assignee_val, author=author,
        )
    if not ok:
        return DecomposeOutcome(task.id, False, "task moved out of triage before promotion")
    return DecomposeOutcome(task.id, True, "single task (no fanout)", fanout=False, new_title=title_val)


def _clean_children(task_id: str, raw_tasks: list, routing: _Routing) -> tuple[list[dict], str]:
    """Validate/normalise the LLM's ``tasks`` list; ``(children, "")`` or ``([], reason)``.
    Unknown assignees route to the default; never assignee=None."""
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return [], f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee, default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
        if isinstance(assignee, str) and assignee.strip() and assignee.strip() not in routing.valid_names:
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, routing.default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        child_entry = {
            "title": title.strip()[:200],
            "body": body.strip() if isinstance(body, str) else "",
            "assignee": chosen,
            # Drop non-int, out-of-range and self parent indices.
            "parents": [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx],
        }
        # Optional per-child priority override; absent/non-int -> inherit the
        # root's priority (handled downstream in decompose_triage_task).
        priority = entry.get("priority")
        if isinstance(priority, int) and not isinstance(priority, bool):
            child_entry["priority"] = priority
        children.append(child_entry)
    return children, ""


def _apply_fanout(task_id: str, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(task_id, False, "decomposer returned fanout=true with empty tasks list")
    children, reason = _clean_children(task_id, raw_tasks, routing)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn,
                task_id,
                root_assignee=routing.orchestrator,
                children=children,
                author=author,
                auto_promote=routing.auto_promote,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return DecomposeOutcome(task_id, False, "task already decomposed or moved out of triage")
    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children", fanout=True, child_ids=child_ids,
    )


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, no aux client, API error, malformed/empty reply, a child
    body that breaks the contract twice) surface as ``ok=False``."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, reason)

    routing = _load_routing()
    user = _USER_TEMPLATE.format(
        **_task_prompt_fields(task),
        roster=_format_roster(routing.roster),
        default_assignee=routing.default_assignee,
    )
    audit_author = author or _profile_author()

    # One retry, then stop. A non-conforming card is worse than no card: it is
    # dispatched to a worker and burns two review rounds before anyone notices,
    # so a failed contract check must never fall through to a silent create.
    violation = ""
    for attempt in (1, 2):
        raw, reason = _call_aux(
            "decompose", task_id, aux_task="kanban_decomposer", system=_SYSTEM_PROMPT,
            user=user if attempt == 1 else user + _RETRY_TEMPLATE.format(
                violation=violation, max_acs=_MAX_ACS_PER_CHILD,
            ),
            max_tokens=4000, timeout=timeout or 180, log=logger,
        )
        if raw is None:
            return DecomposeOutcome(task_id, False, reason)

        parsed = _extract_json_blob(raw, _FENCE_RE)
        if parsed is None:
            return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

        if not parsed.get("fanout"):
            return _apply_single(task, parsed, routing, audit_author)

        violation = _contract_violation(parsed)
        if not violation:
            return _apply_fanout(task_id, parsed, routing, audit_author)
        logger.info(
            "decompose: task %s attempt %d violated the child body contract: %s",
            task_id, attempt, violation,
        )

    return DecomposeOutcome(
        task_id, False, f"child body contract violated after one retry: {violation}",
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column.

    Tasks parked in triage by the unblock-loop breaker for a genuine human-decision gate
    (``block_recurrences >= BLOCK_RECURRENCE_LIMIT`` AND ``block_kind == "needs_input"``) are
    EXCLUDED: ``block_task`` routes a task that re-blocked for the same cause
    ``BLOCK_RECURRENCE_LIMIT`` times to ``triage`` for a human. Triage is also the auto-decompose
    sweep's input queue, so re-decomposing such a task promotes it straight back to ``ready`` →
    claimed → blocked on the same cause → triage again, every tick (seen at ``recurrences: 18``
    against a limit of 2). A ``needs_input`` cause is "a human has not made a decision yet",
    which no amount of re-decomposition resolves, so the automation must not re-arm that loop —
    these stay in triage until a human resolves them.

    A loop-broken task whose last block kind was ``capability``/``transient``/legacy-``None`` is
    NOT excluded: those can be genuine scope/fanout problems (the class decomposition is meant to
    fix), so they remain visible to this sweep."""
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
    return [
        row.id for row in rows
        if not ((row.block_recurrences or 0) >= kb.BLOCK_RECURRENCE_LIMIT and row.block_kind == "needs_input")
    ]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import json  # noqa: F401,E402
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
