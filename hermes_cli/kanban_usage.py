"""Whole-campaign inference accounting for Kanban runs.

``kanban_db._copy_run_session_analytics`` used to be the only accounting path: seven
scalar columns copied once from the assignee profile's ``sessions`` row at ``_end_run``.
That is main-loop-only and one-shot, so it structurally undercounts a campaign:

* **auxiliary calls are invisible.** ``SessionUsageMixin.record_auxiliary_usage``
  deliberately writes vision/compression/title work to ``session_model_usage`` WITHOUT
  touching the ``sessions`` counters, so a ``sessions``-only read can never see them.
* **delegated subagents are invisible.** A child run gets its own session row linked by
  ``parent_session_id``; nothing folded those back into the parent's run.
* **the worker's own tail is missing.** ``kanban_complete`` is a tool call, so the turns
  the worker takes after it — and the token writer's queued deltas — land in ``state.db``
  strictly after ``_end_run`` already snapshotted. A one-shot copy can never catch them.
* **cache writes were not recorded at all**, despite being the expensive bucket.
* **mixed routes collapsed.** Seven flat scalars cannot separate two providers.

The fix is to stop treating the snapshot as final. :func:`reconcile_run_usage` recomputes
a run's usage as an ABSOLUTE per-route set and rewrites ``task_run_usage`` wholesale, so
running it again after a late write simply grows the numbers and running it twice on
unchanged input is byte-identical. Reads re-reconcile before aggregating, which is what
makes a receipt taken at ``_end_run`` and a receipt taken an hour later agree.

Nothing here is outbound telemetry and nothing here exports transcript content: every
read is local SQLite, and the tool-call dedupe parses ids in-process purely to count
DISTINCT ones, persisting only integers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

_log = logging.getLogger("hermes_cli.kanban_db")

# ``task_run_usage`` is the per-route breakdown; ``task_runs`` keeps the rollup so every
# existing reader (CLI, dashboard, Run History) keeps working untouched.
TASK_RUN_USAGE_SCHEMA_SQL = """
-- Per-route inference usage for one Kanban run, reconciled from the assignee
-- profile's state.db. One row per (scope, task, model, provider, base_url,
-- billing_mode) so two providers or a mid-run /model switch never collapse into
-- one another. ``scope``: 'main' = the worker's own session, 'delegated' = a
-- subagent session descended from it. ``task``: '' = main agent loop, otherwise
-- the auxiliary task name ('vision', 'compression', 'title_generation', ...).
-- Rewritten wholesale per run by reconcile_run_usage(), so it is an absolute set
-- and reconciling repeatedly is idempotent.
CREATE TABLE IF NOT EXISTS task_run_usage (
    run_id             INTEGER NOT NULL,
    task_id            TEXT NOT NULL,
    scope              TEXT NOT NULL DEFAULT 'main',
    task               TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT 'unknown',
    provider           TEXT NOT NULL DEFAULT '',
    base_url           TEXT NOT NULL DEFAULT '',
    billing_mode       TEXT NOT NULL DEFAULT '',
    api_calls          INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens   INTEGER,
    estimated_cost_usd REAL,
    -- 'model_usage' = per-route rows from session_model_usage; 'session_row' =
    -- degraded fallback synthesized from the flat sessions counters on an older
    -- state schema that has no session_model_usage table.
    source             TEXT NOT NULL DEFAULT 'model_usage',
    updated_at         INTEGER NOT NULL,
    PRIMARY KEY (run_id, scope, task, model, provider, base_url, billing_mode)
);
CREATE INDEX IF NOT EXISTS idx_task_run_usage_task ON task_run_usage(task_id);
"""

# Additive ``task_runs`` columns this module owns. Kept in lockstep with SCHEMA_SQL's
# CREATE TABLE task_runs and kanban_db_connect._LATER_RUN_COLUMNS.
TASK_RUN_USAGE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("cache_write_tokens", "cache_write_tokens INTEGER"),
    ("usage_status", "usage_status TEXT"),
)

# Token buckets summed identically everywhere (rollup, campaign aggregate, receipts).
TOKEN_FIELDS: Tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
)
# Every countable field, tokens plus call counts. ``estimated_cost_usd`` is a float and
# is summed separately so an all-unavailable campaign stays None rather than becoming 0.0.
COUNT_FIELDS: Tuple[str, ...] = TOKEN_FIELDS + ("api_calls",)

# usage_status values. 'reconciled' = per-route rows from session_model_usage;
# 'session_only' = degraded, flat sessions counters only (older state schema);
# 'unavailable' = nothing readable, every count stays NULL. NULL columns mean
# "not measurable", 0 means "measured zero" — the two are never conflated.
STATUS_RECONCILED = "reconciled"
STATUS_SESSION_ONLY = "session_only"
STATUS_UNAVAILABLE = "unavailable"


class UsageUnavailable(Exception):
    """The profile's session store could not be read for this run."""


# ── profile state.db access ──────────────────────────────────────────────────


def _open_profile_state(profile: str) -> sqlite3.Connection:
    """Read-only connection to a profile's ``state.db``.

    Read-only on purpose: reconciliation must never be able to mutate a worker's
    session store, and a read-only handle also cannot take a write lock away from a
    live worker that is still recording usage.
    """
    from hermes_cli.profiles import resolve_profile_env

    state_path = Path(resolve_profile_env(profile)) / "state.db"
    if not state_path.exists():
        raise UsageUnavailable(f"no state.db for profile {profile!r}")
    conn = sqlite3.connect(f"{state_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _descendant_session_ids(conn: sqlite3.Connection, session_id: str) -> List[str]:
    """``session_id`` plus every session transitively descended from it.

    Delegated subagents and in-place compaction children both hang off
    ``parent_session_id``; their usage is part of what the run actually spent, so a
    receipt that omits them undercounts. Walked iteratively with a visited set rather
    than via a recursive CTE so a cyclic/self-referential row (seen on repaired stores)
    terminates instead of hanging.

    A store old enough to lack ``parent_session_id`` has no lineage to walk, so it
    degrades to the single session rather than failing the whole reconciliation.
    """
    if "parent_session_id" not in _columns(conn, "sessions"):
        return [session_id]
    seen: Set[str] = {session_id}
    frontier = [session_id]
    while frontier:
        batch, frontier = frontier, []
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT id FROM sessions WHERE parent_session_id IN ({placeholders})", batch
        ).fetchall()
        for row in rows:
            child = str(row["id"])
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    # session_id first, the rest sorted, so the reconciled row order is deterministic.
    return [session_id] + sorted(seen - {session_id})


def _distinct_tool_calls(conn: sqlite3.Connection, session_ids: Sequence[str]) -> Optional[int]:
    """Count DISTINCT tool-call ids across *session_ids*.

    ``sessions.tool_call_count`` is a bump counter: compaction clones tail rows
    byte-exactly (``_clone_message_rows``) and retries re-append, so it over-reports the
    number of tool OPERATIONS. Deduplicating by the provider's own tool-call id gives
    exactly-once counting, which is what "deduplicate by tool ID" means.

    Inactive rows are scanned too — a tool call compacted out of the live transcript
    still happened — and the id dedupe is what keeps its clone from counting twice.
    Entries carrying no id (non-OpenAI shapes) have no key to dedupe on, so they are
    counted once per ``active = 1`` row instead.

    With no ``messages`` table to dedupe against we fall back to the flat
    ``tool_call_count``: over-reporting is better than reporting nothing, and the run's
    ``usage_status`` already says the data is degraded.

    Ids are parsed in-process and discarded; only the integer count is ever returned.
    No message content, tool name, or argument ever leaves this function.
    """
    if not session_ids:
        return None
    if not _has_table(conn, "messages"):
        if "tool_call_count" not in _columns(conn, "sessions"):
            return None
        placeholders = ",".join("?" for _ in session_ids)
        row = conn.execute(
            f"SELECT SUM(COALESCE(tool_call_count, 0)) FROM sessions "
            f"WHERE id IN ({placeholders})",
            list(session_ids),
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"SELECT tool_calls, active FROM messages "
        f"WHERE session_id IN ({placeholders}) AND tool_calls IS NOT NULL",
        list(session_ids),
    ).fetchall()
    ids: Set[str] = set()
    unkeyed = 0
    for row in rows:
        try:
            parsed = json.loads(row["tool_calls"])
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            call_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(call_id, str) and call_id:
                ids.add(call_id)
            elif row["active"]:
                unkeyed += 1
    return len(ids) + unkeyed


def _route_rows(
    conn: sqlite3.Connection, main_session_id: str, session_ids: Sequence[str],
) -> Tuple[List[Dict[str, Any]], str]:
    """Per-route usage rows for *session_ids* -> ``(rows, source)``.

    Prefers ``session_model_usage``, which is the only table carrying auxiliary
    (``task != ''``) usage and per-(model, provider) separation. Falls back to the flat
    ``sessions`` counters on an older state schema, which is honestly degraded: it has
    no aux rows and no route split, so it is labelled ``session_only`` rather than
    silently passed off as a full reconciliation.
    """
    if _has_table(conn, "session_model_usage"):
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"""
            SELECT session_id, task, model, billing_provider, billing_base_url,
                   billing_mode, api_call_count, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd
              FROM session_model_usage
             WHERE session_id IN ({placeholders})
            """,
            list(session_ids),
        ).fetchall()
        return (
            [
                {
                    "scope": "main" if str(r["session_id"]) == main_session_id else "delegated",
                    "task": r["task"] or "",
                    "model": r["model"] or "unknown",
                    "provider": r["billing_provider"] or "",
                    "base_url": r["billing_base_url"] or "",
                    "billing_mode": r["billing_mode"] or "",
                    "api_calls": int(r["api_call_count"] or 0),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_write_tokens": int(r["cache_write_tokens"] or 0),
                    "reasoning_tokens": int(r["reasoning_tokens"] or 0),
                    "estimated_cost_usd": float(r["estimated_cost_usd"] or 0.0),
                }
                for r in rows
            ],
            "model_usage",
        )

    available = _columns(conn, "sessions")
    # A legacy store may lack route/cache columns entirely; select only what exists and
    # treat the rest as absent rather than failing the whole reconciliation.
    optional = ("model", "billing_provider", "billing_base_url", "billing_mode",
                "api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd")
    selected = [c for c in optional if c in available]
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"SELECT id{''.join(', ' + c for c in selected)} FROM sessions "
        f"WHERE id IN ({placeholders})",
        list(session_ids),
    ).fetchall()

    def _get(row, column, default: Any = 0) -> Any:
        return row[column] if column in selected else default

    return (
        [
            {
                "scope": "main" if str(r["id"]) == main_session_id else "delegated",
                "task": "",
                "model": _get(r, "model", None) or "unknown",
                "provider": _get(r, "billing_provider", None) or "",
                "base_url": _get(r, "billing_base_url", None) or "",
                "billing_mode": _get(r, "billing_mode", None) or "",
                "api_calls": int(_get(r, "api_call_count") or 0),
                "input_tokens": int(_get(r, "input_tokens") or 0),
                "output_tokens": int(_get(r, "output_tokens") or 0),
                "cache_read_tokens": int(_get(r, "cache_read_tokens") or 0),
                "cache_write_tokens": int(_get(r, "cache_write_tokens") or 0),
                "reasoning_tokens": int(_get(r, "reasoning_tokens") or 0),
                "estimated_cost_usd": float(_get(r, "estimated_cost_usd", None) or 0.0),
            }
            for r in rows
        ],
        "session_row",
    )


def _merge_routes(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse rows sharing a route key, summing counts.

    Two delegated sessions on the same model/provider are one route as far as a receipt
    is concerned, and the primary key requires the collapse anyway.
    """
    merged: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for row in rows:
        key = (row["scope"], row["task"], row["model"], row["provider"],
               row["base_url"], row["billing_mode"])
        target = merged.get(key)
        if target is None:
            merged[key] = dict(row)
            continue
        for field in COUNT_FIELDS:
            target[field] += row[field]
        target["estimated_cost_usd"] += row["estimated_cost_usd"]
    return [merged[key] for key in sorted(merged)]


# ── reconciliation ───────────────────────────────────────────────────────────


def _empty_receipt(run_id: Optional[int], status: str) -> Dict[str, Any]:
    """A receipt with every count NULL — 'not measurable', never a truthful zero."""
    return {
        "run_id": run_id, "usage_status": status, "routes": [],
        "tool_calls": None, "estimated_cost_usd": None,
        **{field: None for field in COUNT_FIELDS},
    }


def reconcile_run_usage(conn: sqlite3.Connection, run_id: int) -> Dict[str, Any]:
    """Recompute one run's usage from its profile's ``state.db`` and persist it.

    Absolute-set semantics: ``task_run_usage`` rows for this run are replaced wholesale
    and the ``task_runs`` rollup is overwritten, never incremented. That is what makes
    this idempotent (reconciling unchanged input twice is byte-identical) AND
    late-write-correct (a worker tail that landed after ``_end_run`` is simply included
    the next time, instead of being double-counted or lost).

    Runs inside the caller's transaction. Raises only on a programming error; an
    unreadable/absent session store is recorded as ``usage_status='unavailable'`` with
    NULL counts. Callers on a lifecycle path must still wrap this — see
    :func:`safe_reconcile_run_usage`, which is what the lifecycle actually calls.
    """
    import time

    row = conn.execute(
        "SELECT task_id, profile, session_id FROM task_runs WHERE id = ?", (int(run_id),)
    ).fetchone()
    if row is None:
        return _empty_receipt(run_id, STATUS_UNAVAILABLE)
    task_id = str(row["task_id"])
    profile, session_id = row["profile"], row["session_id"]

    routes: List[Dict[str, Any]] = []
    tool_calls: Optional[int] = None
    status = STATUS_UNAVAILABLE
    if profile and session_id:
        state_conn = None
        try:
            state_conn = _open_profile_state(str(profile))
            session_ids = _descendant_session_ids(state_conn, str(session_id))
            raw, source = _route_rows(state_conn, str(session_id), session_ids)
            if not raw:
                raise UsageUnavailable("no usage rows for session")
            routes = _merge_routes(raw)
            for route in routes:
                route["source"] = source
            tool_calls = _distinct_tool_calls(state_conn, session_ids)
            status = STATUS_RECONCILED if source == "model_usage" else STATUS_SESSION_ONLY
        except UsageUnavailable as exc:
            _log.debug(
                "kanban run analytics unavailable for run=%s profile=%s session=%s (%s)",
                run_id, profile, session_id, exc,
            )
        except sqlite3.Error as exc:
            _log.debug(
                "kanban run analytics unavailable for run=%s profile=%s session=%s (%s)",
                run_id, profile, session_id, exc,
            )
    else:
        _log.debug(
            "kanban run analytics unavailable for run=%s profile=%s session=%s "
            "(run carries no profile/session)", run_id, profile, session_id,
        )

    now = int(time.time())
    # Absolute set: clear first so a route that disappeared cannot linger and a repeat
    # reconciliation cannot accumulate.
    conn.execute("DELETE FROM task_run_usage WHERE run_id = ?", (int(run_id),))
    for route in routes:
        conn.execute(
            """
            INSERT INTO task_run_usage (
                run_id, task_id, scope, task, model, provider, base_url, billing_mode,
                api_calls, input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, estimated_cost_usd, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(run_id), task_id, route["scope"], route["task"], route["model"],
                route["provider"], route["base_url"], route["billing_mode"],
                route["api_calls"], route["input_tokens"], route["output_tokens"],
                route["cache_read_tokens"], route["cache_write_tokens"],
                route["reasoning_tokens"], route["estimated_cost_usd"],
                route["source"], now,
            ),
        )

    receipt = _empty_receipt(run_id, status)
    if routes:
        for field in COUNT_FIELDS:
            receipt[field] = sum(route[field] for route in routes)
        receipt["estimated_cost_usd"] = sum(route["estimated_cost_usd"] for route in routes)
        receipt["routes"] = routes
    receipt["tool_calls"] = tool_calls
    receipt["task_id"] = task_id

    conn.execute(
        """
        UPDATE task_runs
           SET input_tokens = ?, output_tokens = ?, cache_read_tokens = ?,
               cache_write_tokens = ?, reasoning_tokens = ?, api_calls = ?,
               tool_calls = ?, estimated_cost_usd = ?, usage_status = ?
         WHERE id = ?
        """,
        (
            receipt["input_tokens"], receipt["output_tokens"], receipt["cache_read_tokens"],
            receipt["cache_write_tokens"], receipt["reasoning_tokens"], receipt["api_calls"],
            receipt["tool_calls"], receipt["estimated_cost_usd"], status, int(run_id),
        ),
    )
    return receipt


def safe_reconcile_run_usage(conn: sqlite3.Connection, run_id: int) -> Optional[Dict[str, Any]]:
    """:func:`reconcile_run_usage` reduced to a debug line on ANY failure.

    FINAL decision on this card: a lifecycle transition must succeed even when analytics
    persistence does not. Completion, review, rework and the crash path all call this,
    never the bare reconciler, so no accounting defect can strand a task mid-transition.
    Broad by design — the alternative is a worker that cannot finish because a usage
    table is missing.
    """
    try:
        return reconcile_run_usage(conn, run_id)
    except Exception as exc:
        _log.debug("kanban run analytics unavailable for run=%s (%s)", run_id, exc)
        return None


# ── campaign aggregation ─────────────────────────────────────────────────────


def campaign_root_ids(conn: sqlite3.Connection, task_id: str) -> List[str]:
    """Parentless ancestors reachable from *task_id* by walking ``task_links`` upward.

    A campaign is a DAG, not a tree (fan-in on a synthesizer is normal), so there can be
    several roots and the answer is a list. A task with no parents is its own root.
    """
    seen: Set[str] = {task_id}
    roots: Set[str] = set()
    frontier = [task_id]
    while frontier:
        batch, frontier = frontier, []
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT child_id, parent_id FROM task_links WHERE child_id IN ({placeholders})",
            batch,
        ).fetchall()
        has_parent = {str(r["child_id"]) for r in rows}
        roots.update(t for t in batch if t not in has_parent)
        for row in rows:
            parent = str(row["parent_id"])
            if parent not in seen:
                seen.add(parent)
                frontier.append(parent)
    return sorted(roots)


def campaign_task_ids(conn: sqlite3.Connection, task_id: str) -> List[str]:
    """Every task in *task_id*'s campaign: the roots plus their descendant closure.

    Walking DOWN from the roots (rather than just up from the task) is what pulls in the
    sibling phases the card enumerates — specification, implementation, review, rework,
    landing and required children are each a task in that closure, so they are covered
    structurally without hardcoding a phase taxonomy.
    """
    seen: Set[str] = set(campaign_root_ids(conn, task_id)) or {task_id}
    frontier = list(seen)
    while frontier:
        batch, frontier = frontier, []
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT child_id FROM task_links WHERE parent_id IN ({placeholders})", batch
        ).fetchall()
        for row in rows:
            child = str(row["child_id"])
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return sorted(seen)


def campaign_usage(
    conn: sqlite3.Connection, task_id: str, *, reconcile: bool = True,
) -> Dict[str, Any]:
    """Whole-campaign inference receipt rooted at *task_id*'s campaign.

    Every run of every task in the campaign is counted exactly ONCE — the aggregate is
    keyed on ``run_id``, so a task retried three times contributes three distinct runs
    and never the same run twice. Auxiliary and delegated usage ride along because
    :func:`reconcile_run_usage` already folded them into each run's routes.

    *reconcile* re-runs reconciliation for each run before aggregating, which is what
    picks up worker tails written after the run was finalized; pass ``False`` on a
    read-only connection (the dashboard) to read the last persisted values instead.

    Returns integers and route breakdowns only — no transcript content.
    """
    task_ids = campaign_task_ids(conn, task_id)
    placeholders = ",".join("?" for _ in task_ids)
    run_rows = conn.execute(
        f"SELECT id FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id", task_ids,
    ).fetchall()
    run_ids = [int(r["id"]) for r in run_rows]

    if reconcile:
        for run_id in run_ids:
            safe_reconcile_run_usage(conn, run_id)

    totals: Dict[str, Any] = {field: 0 for field in COUNT_FIELDS}
    totals["estimated_cost_usd"] = 0.0
    totals["tool_calls"] = 0
    by_route: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    measured_runs = 0
    unavailable_runs: List[int] = []

    if run_ids:
        run_placeholders = ",".join("?" for _ in run_ids)
        for row in conn.execute(
            f"SELECT * FROM task_run_usage WHERE run_id IN ({run_placeholders})", run_ids,
        ).fetchall():
            key = (row["scope"], row["task"], row["model"], row["provider"],
                   row["base_url"], row["billing_mode"])
            target = by_route.setdefault(key, {
                "scope": row["scope"], "task": row["task"], "model": row["model"],
                "provider": row["provider"], "base_url": row["base_url"],
                "billing_mode": row["billing_mode"], "estimated_cost_usd": 0.0,
                **{field: 0 for field in COUNT_FIELDS},
            })
            for field in COUNT_FIELDS:
                value = int(row[field] or 0)
                target[field] += value
                totals[field] += value
            cost = float(row["estimated_cost_usd"] or 0.0)
            target["estimated_cost_usd"] += cost
            totals["estimated_cost_usd"] += cost

        for row in conn.execute(
            f"SELECT id, tool_calls, usage_status FROM task_runs WHERE id IN ({run_placeholders})",
            run_ids,
        ).fetchall():
            if (row["usage_status"] or STATUS_UNAVAILABLE) == STATUS_UNAVAILABLE:
                unavailable_runs.append(int(row["id"]))
            else:
                measured_runs += 1
            totals["tool_calls"] += int(row["tool_calls"] or 0)

    if measured_runs == 0:
        # Nothing was measurable anywhere: report NULLs rather than a fabricated zero.
        totals = {field: None for field in COUNT_FIELDS}
        totals["estimated_cost_usd"] = None
        totals["tool_calls"] = None

    return {
        "campaign_roots": campaign_root_ids(conn, task_id),
        "task_ids": task_ids,
        "run_ids": run_ids,
        "runs_measured": measured_runs,
        "runs_unavailable": sorted(unavailable_runs),
        "totals": totals,
        "by_route": [by_route[key] for key in sorted(by_route)],
    }
