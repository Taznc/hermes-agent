"""Profile-aware observation of the actual owned-work registries, not PID guesses."""

from __future__ import annotations

import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

from hermes_constants import get_hermes_home, hermes_home_key


def owner_home(home=""):
    """Freeze producer profile provenance when work is registered/checkpointed."""
    return str(home or get_hermes_home())


def home_matches(home, db):
    return hermes_home_key(home or get_hermes_home()) == hermes_home_key(
        Path(db.db_path).parent
    )


def agent_home(agent):
    db = getattr(agent, "_session_db", None)
    return Path(db.db_path).parent if db is not None else None


def record_home(record):
    owner = record.get("owner_session_record") or {}
    return (
        record.get("owner_profile_home")
        or record.get("profile_home")
        or owner.get("profile_home")
        or agent_home(record.get("agent"))
        or (
            record["_context"].copy().run(get_hermes_home)
            if record.get("_context") is not None
            else None
        )
    )


def live_aliases(sid, record):
    return {
        sid,
        record.get("session_key"),
        getattr(record.get("agent"), "session_id", None),
    } - {None, ""}


def process_active(registry, process):
    """Observe canonical handles/fingerprinted recovery state without reaping or writing."""
    if process.exited:
        return False
    if process.detached and process.pid_scope == "host":
        return (
            registry._detached_host_fate(process.pid, process.host_start_time)
            == "running"
        )
    handle = getattr(process, "process", None)
    if handle is not None:
        try:
            return handle.poll() is None
        except Exception:
            return True  # inability to observe owned work must not authorize an archive
    return True  # PTY/sandbox work remains live until its authoritative reader settles.


@contextmanager
def owned_work_scope(db, session_id, aliases=()):
    """Acquire producer locks BEFORE the writer lock (the normal history -> DB order).

    SQLite BEGIN IMMEDIATE fences durable turn/compression ownership; registry locks
    fence in-process registration through commit. No work is stopped or closed.
    """
    from . import lineage_ids

    with db._read_ctx() as conn:
        ids = lineage_ids(conn, session_id) | set(aliases)
    srv = sys.modules.get("tui_gateway.server")
    children = sys.modules.get("tools.delegate_tool_registry")
    async_work = sys.modules.get("tools.async_delegation")
    processes = sys.modules.get("tools.process_registry")
    registry = getattr(processes, "process_registry", None)
    live = []
    if srv is not None:
        with srv._sessions_lock:
            live = [
                (sid, record)
                for sid, record in srv._sessions.items()
                if home_matches(
                    record.get("profile_home") or agent_home(record.get("agent")), db
                )
                and live_aliases(sid, record) & ids
            ]
    with ExitStack() as stack:
        seen = set()
        for _, record in sorted(live):
            lock = record.get("history_lock")
            if lock is not None and id(lock) not in seen:
                stack.enter_context(lock)
                seen.add(id(lock))
        changed = False
        if srv is not None:
            stack.enter_context(srv._sessions_lock)
            current = [
                (sid, record)
                for sid, record in srv._sessions.items()
                if home_matches(
                    record.get("profile_home") or agent_home(record.get("agent")), db
                )
                and live_aliases(sid, record) & ids
            ]
            changed = {sid: id(record) for sid, record in current} != {
                sid: id(record) for sid, record in live
            }
        for lock in (
            getattr(children, "_active_subagents_lock", None),
            getattr(async_work, "_records_lock", None),
            getattr(registry, "_lock", None),
        ):
            if lock is not None:
                stack.enter_context(lock)
        requests = sys.modules.get("tui_gateway.server_requests")
        if requests is not None:
            stack.enter_context(requests._lock)
        api = sys.modules.get("hermes_fork.session_archive.api")
        if api is not None:
            stack.enter_context(api._lock)
        work = WorkSnapshot(db, live, children, async_work, registry)
        work.changed = changed
        yield work


class WorkSnapshot:
    def __init__(self, db, live, children, async_work, processes):
        self.db, self.live = db, live
        self.children, self.async_work, self.processes = children, async_work, processes
        self.changed = False

    def blockers(self, conn, session_id):
        from hermes_state_sessions import _collect_delegate_child_ids

        from . import lineage_ids

        ids = lineage_ids(conn, session_id)
        ids.update(_collect_delegate_child_ids(conn, list(ids)))
        aliases = set(ids)
        result = ["ownership_changed_retry"] if self.changed else []
        for sid, record in self.live:
            aliases.update(live_aliases(sid, record))
            if record.get("running"):
                result.append("running_turn")
            if record.get("starting"):
                result.append("starting_turn")
            if record.get("queued_prompt"):
                result.append("queued_prompt")
            requests = sys.modules.get("tui_gateway.server_requests")
            # The canonical request registry is held through commit, including plugin.request.
            pending = requests is not None and any(
                req.sid == sid for req in requests._open.values()
            )
            if record.get("pending_prompt") or pending:
                result.append("waiting_request")
        if self.children is not None:
            for record in self.children._active_subagents.values():
                if home_matches(record_home(record), self.db) and (
                    record.get("owner_agent_session_id") in ids
                    or record.get("owner_session_id") in aliases
                    or any(
                        self.children._is_descendant_of(
                            record.get("agent"), owner.get("agent")
                        )
                        for _, owner in self.live
                    )
                ):
                    result.append("live_subagent")
        if self.async_work is not None:
            for record in self.async_work._records.values():
                if (
                    home_matches(record_home(record), self.db)
                    and record.get("status") in self.async_work._LIVE_STATES
                    and (
                        record.get("parent_session_id") in ids
                        or record.get("origin_ui_session_id") in aliases
                        or record.get("session_key") in aliases
                    )
                ):
                    result.append("async_delegation")
        if self.processes is not None:
            for process in self.processes._running.values():
                if (
                    home_matches(process.profile_home, self.db)
                    and (
                        process.parent_session_id in ids
                        or process.session_key in aliases
                    )
                    and process_active(self.processes, process)
                ):
                    result.append("background_process")
        api = sys.modules.get("hermes_fork.session_archive.api")
        if api is not None:
            result.extend(api.blockers(self.db, ids))
        import hermes_fork.session_archive.durable as durable_work

        result.extend(durable_work.blockers(self.db, aliases, conn))
        return result
