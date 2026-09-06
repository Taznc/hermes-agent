"""Kanban Swarm v1: thin swarm topology helpers on top of Kanban.

Deliberately no second scheduler — a small task graph written into the
existing Kanban kernel:

    planning root (completed immediately)
        ├─ parallel specialist workers (ready)
        └─ verifier (todo until all workers done)
             └─ synthesizer (todo until verifier done)

The shared blackboard is structured JSON comments on the root task, so all
state lives in existing task_comments/task_events rows and the dashboard,
notifier, slash command and dispatcher keep working without a new service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import sqlite3
import time
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb

BLACKBOARD_PREFIX = "[swarm:blackboard] "


@dataclass(frozen=True)
class SwarmWorkerSpec:
    """A single parallel worker card in a swarm."""

    profile: str
    title: str
    body: str
    skills: list[str] = field(default_factory=list)
    priority: int = 0
    max_runtime_seconds: Optional[int] = None


@dataclass(frozen=True)
class SwarmCreated:
    """IDs produced by :func:`create_swarm`."""

    root_id: str
    worker_ids: list[str]
    verifier_id: str
    synthesizer_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require_text(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _swarm_context(root_id: str, goal: str) -> str:
    return (
        f"\n\n## Swarm protocol\n- Swarm root / shared blackboard: `{root_id}`.\n- Read "
        f"sibling/parent handoffs from Kanban context before working.\n- Put machine-readable "
        f"facts in completion metadata.\n- Put cross-worker notes on the root task using "
        f"structured comments.\n- Goal: {goal.strip()}\n"
    )


def _route_task_kwargs(route: Any) -> dict[str, Any]:
    return {
        "model_override": route.model_override,
        "provider_override": route.provider_override,
        "reasoning_effort": route.reasoning_effort,
        "route_source": route.route_source,
        "route_name": route.route_name,
    }


def _existing_swarm_from_idempotency_key(
    conn: sqlite3.Connection, idempotency_key: Optional[str]
) -> tuple[Optional[SwarmCreated], Optional[str]]:
    if not idempotency_key:
        return None, None
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None, None
    root_id = str(row["id"])
    existing = latest_blackboard(conn, root_id).get("topology")
    if isinstance(existing, dict):
        worker_ids = [str(x) for x in existing.get("worker_ids", []) if x]
        verifier_id = existing.get("verifier_id")
        synthesizer_id = existing.get("synthesizer_id")
        if worker_ids and verifier_id and synthesizer_id:
            return SwarmCreated(root_id, worker_ids, str(verifier_id), str(synthesizer_id)), root_id
    return None, root_id


def _activate_root_inline(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    summary: str,
    metadata: dict[str, Any],
) -> bool:
    """Inline blocked→done CAS flip + event insert for the swarm root.

    Runs INSIDE create_swarm's write_txn, so it must not call
    ``kb.complete_task`` (own transaction + post-commit side effects that
    would run while the outer txn can still roll back). The caller runs
    ``recompute_ready`` after the outer commit.
    """
    cur = conn.execute(
        """
        UPDATE tasks
           SET status       = 'done',
               completed_at = ?,
               claim_lock   = NULL,
               claim_expires= NULL,
               worker_pid   = NULL
         WHERE id = ?
           AND status = 'blocked'
        """,
        (int(time.time()), root_id),
    )
    if cur.rowcount != 1:
        return False
    run_id = kb._synthesize_ended_run(conn, root_id, outcome="completed", summary=summary, metadata=metadata)
    kb._append_event(
        conn, root_id, "completed", {"result_len": 0, "summary": summary[:400] or None}, run_id=run_id,
    )
    return True


def create_swarm(
    conn: sqlite3.Connection,
    *,
    goal: str,
    workers: Iterable[SwarmWorkerSpec],
    verifier_assignee: str,
    synthesizer_assignee: str,
    root_title: Optional[str] = None,
    verifier_title: str = "Verify swarm outputs",
    synthesizer_title: str = "Synthesize swarm outputs",
    tenant: Optional[str] = None,
    created_by: str = "swarm-orchestrator",
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    priority: int = 0,
    idempotency_key: Optional[str] = None,
) -> SwarmCreated:
    """Atomically create a durable, immediately dispatchable Kanban swarm."""
    goal = _require_text(goal, "goal")
    verifier_assignee = _require_text(verifier_assignee, "verifier_assignee")
    synthesizer_assignee = _require_text(synthesizer_assignee, "synthesizer_assignee")
    worker_specs = list(workers)
    if not worker_specs:
        raise ValueError("at least one worker is required")
    for i, spec in enumerate(worker_specs, start=1):
        _require_text(spec.profile, f"workers[{i}].profile")
        _require_text(spec.title, f"workers[{i}].title")

    existing, existing_root_id = _existing_swarm_from_idempotency_key(conn, idempotency_key)
    if existing is not None:
        return existing

    from hermes_cli.kanban_model_routing import resolve_kanban_model_route

    activation_summary = "Swarm topology planned; root remains the shared blackboard."
    root_title_value = root_title or f"Swarm: {goal.splitlines()[0][:80]}"
    root_body = (
        "Kanban Swarm v1 planning/root card. This card is completed "
        "immediately so parallel workers can start while it remains the "
        f"shared blackboard and audit anchor.\n\nGoal:\n{goal}"
    )
    root_route = None
    if existing_root_id is None:
        root_route = resolve_kanban_model_route(title=root_title_value, body=root_body)
    worker_routes = [
        resolve_kanban_model_route(title=spec.title, body=(spec.body or ""))
        for spec in worker_specs
    ]
    verifier_body = (
        "Review every worker handoff and blackboard update. Gate the swarm: "
        "complete only with metadata {\"gate\": \"pass\"} when evidence is "
        "sufficient; otherwise block with exact missing work."
    )
    verifier_route = resolve_kanban_model_route(title=verifier_title, body=verifier_body)
    synthesizer_body = (
        "Synthesize the verified worker outputs into the final deliverable. "
        "Do not start until the verifier has passed the gate."
    )
    synthesizer_route = resolve_kanban_model_route(title=synthesizer_title, body=synthesizer_body)

    activated = False
    with kb.write_txn(conn):
        root = kb.create_task(
            conn,
            title=root_title_value,
            body=root_body,
            assignee=created_by,
            priority=priority,
            idempotency_key=idempotency_key,
            initial_status="blocked",
            **({} if root_route is None else _route_task_kwargs(root_route)),
            created_by=created_by,
            tenant=tenant,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        existing_after_root = latest_blackboard(conn, root).get("topology")
        if isinstance(existing_after_root, dict):
            worker_ids = [str(x) for x in existing_after_root.get("worker_ids", []) if x]
            verifier_id = existing_after_root.get("verifier_id")
            synthesizer_id = existing_after_root.get("synthesizer_id")
            if worker_ids and verifier_id and synthesizer_id:
                return SwarmCreated(root, worker_ids, str(verifier_id), str(synthesizer_id))

        context_suffix = _swarm_context(root, goal)
        worker_ids = []
        for spec, route in zip(worker_specs, worker_routes):
            worker_ids.append(
                kb.create_task(
                    conn,
                    title=spec.title,
                    body=(spec.body or "") + context_suffix,
                    assignee=spec.profile,
                    parents=[root],
                    priority=spec.priority or priority,
                    skills=spec.skills or None,
                    max_runtime_seconds=spec.max_runtime_seconds,
                    **_route_task_kwargs(route),
                    created_by=created_by,
                    tenant=tenant,
                    workspace_kind=workspace_kind,
                    workspace_path=workspace_path,
                )
            )
        verifier = kb.create_task(
            conn,
            title=verifier_title,
            body=verifier_body + context_suffix,
            assignee=verifier_assignee,
            parents=worker_ids,
            priority=priority,
            skills=["requesting-code-review"],
            **_route_task_kwargs(verifier_route),
            created_by=created_by,
            tenant=tenant,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        synthesizer = kb.create_task(
            conn,
            title=synthesizer_title,
            body=synthesizer_body + context_suffix,
            assignee=synthesizer_assignee,
            parents=[verifier],
            priority=priority,
            skills=["humanizer"],
            **_route_task_kwargs(synthesizer_route),
            created_by=created_by,
            tenant=tenant,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )

        created = SwarmCreated(root, worker_ids, verifier, synthesizer)
        post_blackboard_update(conn, root, author=created_by, key="topology", value=created.as_dict() | {"goal": goal})
        root_row = kb.get_task(conn, root)
        if root_row is not None and root_row.status == "blocked":
            if not _activate_root_inline(
                conn,
                root,
                summary=activation_summary,
                metadata={
                    "kind": "kanban_swarm_v1",
                    "goal": goal.strip(),
                    "worker_count": len(created.worker_ids),
                },
            ):
                raise RuntimeError("could not activate the completed swarm topology")
            activated = True
    if activated:
        # After commit: recompute_ready opens its own txn and must never run
        # under an open write_txn.
        kb.recompute_ready(conn)
        root = kb.get_task(conn, created.root_id)
        run = kb.latest_run(conn, created.root_id)
        kb._fire_kanban_lifecycle_hook(
            "kanban_task_completed",
            created.root_id,
            board=kb.get_current_board(),
            assignee=root.assignee if root else None,
            run_id=run.id if run else None,
            summary=activation_summary,
        )
    return created


def post_blackboard_update(conn: sqlite3.Connection, root_id: str, *, author: str, key: str, value: Any) -> int:
    """Append one structured update to the swarm root blackboard."""
    _require_text(root_id, "root_id")
    author = _require_text(author, "author")
    key = _require_text(key, "key")
    payload = json.dumps({"key": key, "value": value}, ensure_ascii=False, sort_keys=True)
    return kb.add_comment(conn, root_id, author=author, body=BLACKBOARD_PREFIX + payload)


def latest_blackboard(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    """Merge structured blackboard comments on a root card. Later comments
    replace earlier values for the same key; ``_authors`` records the author
    of the winning value for traceability."""
    merged: dict[str, Any] = {}
    authors: dict[str, str] = {}
    for comment in kb.list_comments(conn, root_id):
        body = comment.body or ""
        if not body.startswith(BLACKBOARD_PREFIX):
            continue
        try:
            payload = json.loads(body[len(BLACKBOARD_PREFIX):])
        except json.JSONDecodeError:
            continue
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            continue
        merged[key] = payload.get("value")
        authors[key] = comment.author
    if authors:
        merged["_authors"] = authors
    return merged


def parse_worker_arg(raw: str) -> SwarmWorkerSpec:
    """Parse CLI ``--worker profile:title[:skill,skill]`` values."""
    parts = [p.strip() for p in raw.split(":", 2)]
    if len(parts) < 2:
        raise ValueError("worker must be profile:title or profile:title:skill,skill")
    skills = [s.strip() for s in parts[2].split(",") if s.strip()] if len(parts) == 3 and parts[2] else []
    return SwarmWorkerSpec(profile=parts[0], title=parts[1], body=parts[1], skills=skills)
