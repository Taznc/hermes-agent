"""Mergeability preflight for ``kanban_request_review`` (task t_3e83300c).

10% of this fleet's ``changes_requested`` verdicts were one sentence — "merge
current origin/dev, resolve conflicts, re-request review" — costing a full
implementer round plus a full reviewer round to run one ``git merge``. A branch
that cannot merge its land target has nothing a reviewer can usefully adjudicate,
so the check belongs in front of the review lane rather than inside it.

Git decides, not us: ``git merge-tree --write-tree`` performs the real merge in
memory and reports the paths it could not resolve. Everything here is a thin
wrapper around that one answer.

Lives outside ``tools/kanban_tools.py`` because the DB layer must stay
git-agnostic and the tool facade must stay subprocess-agnostic; this is the
only module in the kanban surface that shells out to git.

It also owns the *orchestration* around that answer — the config switch, the
land-target resolution, the workspace lookup, and the conflict event — because
there are two doors into the review lane (``kanban_request_review`` and
``hermes kanban request-review``) and a gate that only guards one of them is
advisory rather than real (task t_11421628). Both entry points call
:func:`preflight` and :func:`record_conflict`; neither owns a copy.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)

# Each git call is bounded: a hung fetch must not wedge a worker's handoff. The
# preflight fails OPEN on timeout, so this is a latency bound, not a gate.
_GIT_TIMEOUT_S = 60


@dataclass(frozen=True)
class Mergeability:
    """The preflight's verdict. ``conflicts`` empty = merges cleanly."""

    target: str
    sha: str
    conflicts: tuple[str, ...]

    @property
    def stamp(self) -> str:
        """``"<remote>/<branch>@<sha>"`` — the string the pre-review gate asks
        workers to paste, so the tool can stamp it instead."""
        return f"{self.target}@{self.sha}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
    )


def _conflicting_paths(stdout: str) -> list[str]:
    """Paths from ``merge-tree --write-tree --name-only`` conflict output: line
    0 is the written tree's oid, then one path per line up to the first blank
    line (git's human-readable messages follow it)."""
    paths: list[str] = []
    for line in stdout.splitlines()[1:]:
        if not line.strip():
            break
        paths.append(line.strip())
    return paths


def check(workspace: Optional[str], target: str) -> Optional[Mergeability]:
    """Merge ``workspace``'s HEAD against ``target`` (``<remote>/<branch>``) in
    memory; ``None`` when the check could not be run at all.

    Fails OPEN by design — a missing workspace, a non-repo, an unreachable
    remote, an unknown branch, an ancient git without ``--write-tree``, or a
    timeout all return ``None`` (logged at debug) so an infrastructure problem
    can never strand a finished implementation outside the review lane. Only
    git's own "I merged it and these paths conflict" answer blocks a handoff.
    """
    if not workspace:
        return None
    cwd = Path(workspace)
    remote, _, branch = target.partition("/")
    try:
        if _git(cwd, "rev-parse", "--git-dir").returncode != 0:
            logger.debug("mergeability preflight skipped: %s is not a git repo", cwd)
            return None
        fetch = _git(cwd, "fetch", remote, branch)
        if fetch.returncode != 0:
            logger.debug("mergeability preflight skipped: fetch %s failed: %s",
                         target, fetch.stderr.strip())
            return None
        rev = _git(cwd, "rev-parse", "FETCH_HEAD")
        if rev.returncode != 0 or not rev.stdout.strip():
            logger.debug("mergeability preflight skipped: FETCH_HEAD unresolvable")
            return None
        sha = rev.stdout.strip()
        merge = _git(cwd, "merge-tree", "--write-tree", "--name-only", sha, "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mergeability preflight skipped: %s", exc)
        return None
    if merge.returncode == 0:
        return Mergeability(target=target, sha=sha, conflicts=())
    if merge.returncode != 1:
        # Not git's clean/conflict contract — an old git, a corrupt repo, an
        # unmerged index. Unknown is not a conflict.
        logger.debug("mergeability preflight skipped: merge-tree rc=%s: %s",
                     merge.returncode, merge.stderr.strip())
        return None
    paths = _conflicting_paths(merge.stdout)
    if not paths:
        # Conflicting but git named nothing we can put in front of the worker;
        # a refusal it cannot act on is worse than no refusal.
        logger.debug("mergeability preflight skipped: conflict with no named paths")
        return None
    return Mergeability(target=target, sha=sha, conflicts=tuple(paths))


def refusal_message(result: Mergeability) -> str:
    """The text the implementer reads. Copy-pasteable on purpose: the whole
    point is that they run one command instead of burning a review round."""
    paths = "\n".join(f"  {p}" for p in result.conflicts)
    return (
        f"kanban_request_review refused: your branch conflicts with {result.target} "
        f"({result.sha[:12]}), so a reviewer could not merge it. Conflicting "
        f"paths:\n{paths}\n\n"
        f"Your task is unchanged and still running. Resolve the drift, re-run your "
        f"tests, then request review again:\n\n"
        f"  git fetch {result.target.replace('/', ' ', 1)}\n"
        f"  git merge {result.target}\n"
    )


def _own_task_env(task_id: str, var: str) -> Optional[str]:
    """``$var`` only when this process is scoped to ``task_id``; else None."""
    return os.environ.get(var) if os.environ.get("HERMES_KANBAN_TASK") == task_id else None


def preflight(task, task_id: str, *, board: Optional[str]) -> Optional[Mergeability]:
    """Mergeability verdict for a review handoff, or ``None`` to skip the gate.

    Shared by both doors into the review lane — the ``kanban_request_review``
    tool handler and ``hermes kanban request-review`` — so a worker refused at
    one cannot walk through the other.

    Skips (byte-identical to the pre-gate behavior) when the operator turned it
    off with ``kanban.require_mergeable_for_review: false``, when the board
    configures no ``land_target``, when the card has no worktree workspace, or
    when git could not answer — see :func:`check` for the fail-open contract.
    """
    if not cfg_get(load_config(), "kanban", "require_mergeable_for_review", default=True):
        return None
    from hermes_cli.kanban_land import LandRefusal, resolve_target

    try:
        remote, branch = resolve_target(None, board=board)
    except LandRefusal as exc:
        logger.debug("mergeability preflight skipped for %s: %s", task_id, exc)
        return None
    workspace = (task.workspace_path if task else None) or _own_task_env(
        task_id, "HERMES_KANBAN_WORKSPACE")
    return check(workspace, f"{remote}/{branch}")


def record_conflict(conn, task_id: str, result: Mergeability, *,
                    run_id: Optional[int] = None) -> None:
    """Durably record a refused handoff so the rounds it saves can be counted."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.write_txn(conn):
        kb._append_event(
            conn, task_id, "review_preflight_conflict",
            {"target": result.target, "sha": result.sha, "paths": list(result.conflicts)},
            run_id=run_id)
