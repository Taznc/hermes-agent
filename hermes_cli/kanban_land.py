"""``hermes kanban land`` — attended landing of an explicitly approved review.

Turns a reviewer-approved card into a verified merge to a configured target,
a non-force push, a receipt on the card, closure, and cleanup. Every gate
fails CLOSED: anything unproven refuses with a machine-readable reason code
rather than merging.

The target is never inferred. It comes from ``--target <remote>/<branch>`` or
from the board's ``land_target`` metadata key and from nowhere else, so an
installation whose fork and upstream both look plausible cannot be landed to
the wrong one by accident.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb


class LandRefusal(Exception):
    """A gate refused. ``reason`` is the stable machine-readable code; the
    message is the human sentence shown on the card and in ``--json``."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ApprovalVerdict:
    """The authoritative reviewer approval a landing is allowed to act on."""

    run_id: int
    reviewer: Optional[str]
    summary: Optional[str]
    metadata: dict
    approved_at: Optional[int]



def resolve_target(explicit: Optional[str], *, board: Optional[str]) -> tuple[str, str]:
    """``(remote, branch)`` from ``--target`` or the board's ``land_target``.

    Refuses rather than guessing: there is no fallback to "the only remote",
    to ``origin``, or to the branch's upstream.
    """
    raw = (explicit or "").strip()
    source = "--target"
    if not raw:
        raw = str(kb.read_board_metadata(board).get("land_target") or "").strip()
        source = "board land_target"
    if not raw:
        raise LandRefusal(
            "no_target",
            "no landing target configured: pass --target <remote>/<branch> or set one with "
            "`hermes kanban boards set-land-target <slug> <remote>/<branch>`",
        )
    remote, sep, branch = raw.partition("/")
    if not sep or not remote.strip() or not branch.strip():
        raise LandRefusal(
            "target_unresolvable",
            f"landing target {raw!r} from {source} is not of the form <remote>/<branch>",
        )
    return remote.strip(), branch.strip()


def _json_dict(raw: Any) -> dict:
    """Tolerant JSON -> dict; anything else becomes ``{}``."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def approval_verdict(conn: sqlite3.Connection, task_id: str) -> ApprovalVerdict:
    """The newest reviewer approval on ``task_id``, or :class:`LandRefusal`.

    An approval is a run that BOTH completed the card and was claimed from the
    ``review`` column — i.e. a reviewer, not the implementer, closed it. A
    ``done`` status proves nothing on its own: an implementer completing their
    own card produces exactly that status with no review run behind it.

    A ``changes_requested`` run newer than the approval invalidates it, so a
    stale approval from an earlier round can never be landed.
    """
    rows = conn.execute(
        "SELECT id, outcome, summary, metadata, ended_at, profile FROM task_runs "
        "WHERE task_id = ? AND outcome IN ('completed', 'changes_requested') "
        "ORDER BY id DESC", (task_id,),
    ).fetchall()

    for row in rows:
        if row["outcome"] == "changes_requested":
            raise LandRefusal(
                "changes_requested_unresolved",
                f"the newest review verdict on {task_id} is 'changes requested' (run "
                f"{row['id']}); the card must be re-reviewed and approved before landing",
            )
        claimed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed' "
            "AND run_id = ? ORDER BY id DESC LIMIT 1", (task_id, int(row["id"])),
        ).fetchone()
        if _json_dict(claimed["payload"] if claimed else None).get("source_status") != "review":
            # A completion that never came from the review column is the
            # implementer closing their own card — not an approval.
            continue
        return ApprovalVerdict(
            run_id=int(row["id"]),
            reviewer=row["profile"],
            summary=row["summary"],
            metadata=_json_dict(row["metadata"]),
            approved_at=row["ended_at"],
        )

    raise LandRefusal(
        "no_approval",
        f"{task_id} has no reviewer approval: landing requires a run claimed from the "
        "review column that completed the card",
    )


# ---------------------------------------------------------------------------
# Git plumbing — every call is explicit, bounded, and never interactive
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """A git invocation failed; carries the command and captured stderr."""


def git(cwd: str, *args: str, timeout: int = 120, check: bool = True) -> str:
    """Run git in ``cwd`` and return stripped stdout.

    ``GIT_TERMINAL_PROMPT=0`` and the ssh batch flag keep a credential prompt
    from hanging an attended command forever on a remote we cannot reach.
    """
    import os
    import subprocess

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(proc.stderr or '').strip()}")
    return (proc.stdout or "").strip()


@dataclass(frozen=True)
class SourceState:
    """The exact, pushed content a landing would merge."""

    task_id: str
    worktree: str
    repo_root: str
    branch: str
    remote: str
    sha: str


def _repo_root_for(worktree: str) -> Optional[str]:
    """The main checkout backing a task worktree, whether or not the worktree
    still exists.

    Approval-time cleanup removes a clean, fully-pushed worktree, so by the
    time landing runs the path is frequently gone. Walking up to the nearest
    directory holding a real ``.git`` directory finds the checkout either way.
    """
    from pathlib import Path as _Path

    p = _Path(worktree).expanduser()
    if p.is_dir():
        try:
            common = git(str(p), "rev-parse", "--git-common-dir")
        except GitError:
            common = ""
        if common:
            common_path = _Path(common)
            if not common_path.is_absolute():
                common_path = p / common_path
            return str(common_path.resolve().parent)
    for parent in p.parents:
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def _worktree_branch(worktree: str) -> Optional[str]:
    """The branch a surviving worktree is on; None when it is gone."""
    from pathlib import Path as _Path

    if not _Path(worktree).is_dir():
        return None
    try:
        return git(worktree, "rev-parse", "--abbrev-ref", "HEAD") or None
    except GitError:
        return None


def _remotes(cwd: str) -> list[str]:
    """Configured remote names, or [] when they cannot be read."""
    try:
        return [n for n in git(cwd, "remote").splitlines() if n.strip()]
    except GitError:
        return []


def _remote_branch_sha(cwd: str, remote: str, branch: str) -> Optional[str]:
    """Read-only remote lookup: the sha ``remote`` currently publishes for
    ``branch``, or None. Writes nothing — not even a remote-tracking ref."""
    try:
        out = git(cwd, "ls-remote", "--heads", remote, f"refs/heads/{branch}", timeout=60)
    except GitError as exc:
        raise LandRefusal(
            "target_unresolvable", f"cannot read remote {remote!r}: {exc}",
        ) from exc
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and sha.strip():
            return sha.strip()
    return None


def source_state(conn: sqlite3.Connection, task_id: str, *, remote: str) -> SourceState:
    """Everything about the card's own branch, or :class:`LandRefusal`.

    Refuses a live worker, a workspace that is not a usable linked worktree, a
    dirty tree, a branch whose local HEAD is not exactly what ``remote``
    publishes, and a remote that does not carry the branch at all. The last one
    is how "you pointed this at the wrong remote" is caught generically: the
    check is that the work is actually THERE, not that the remote has a
    particular name.
    """
    row = conn.execute(
        "SELECT status, claim_lock, workspace_kind, workspace_path, branch_name "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        raise LandRefusal("task_not_found", f"no task {task_id!r} on this board")
    if row["status"] == "running" or row["claim_lock"]:
        raise LandRefusal(
            "live_worker",
            f"{task_id} is claimed by a live worker; landing never races a running run",
        )
    if not kb._parents_satisfied(conn, task_id):
        raise LandRefusal(
            "deps_unsatisfied", f"{task_id} has parent dependencies that are not done",
        )

    worktree = str(row["workspace_path"] or "")
    if row["workspace_kind"] != "worktree" or not worktree:
        raise LandRefusal(
            "workspace_unusable",
            f"{task_id} has no git worktree workspace "
            f"(kind={row['workspace_kind']!r}, path={worktree or 'unset'!r}); "
            "landing needs the tree the work was done in",
        )
    repo_root = _repo_root_for(worktree)
    if repo_root is None:
        raise LandRefusal(
            "workspace_unusable",
            f"{task_id}: no git repository found at or above {worktree}",
        )
    branch = row["branch_name"] or _worktree_branch(worktree)
    if not branch or branch == "HEAD":
        raise LandRefusal(
            "workspace_unusable",
            f"{task_id}: worktree {worktree} is not on a named branch",
        )

    published = _remote_branch_sha(repo_root, remote, branch)
    if published is None:
        # Distinguish "never pushed anywhere" from "pushed, but to a different
        # remote than the one you aimed this at" — the second is the
        # fork-vs-upstream mistake and deserves its own reason code. Neither
        # names a specific remote: the rule is that the work must live on the
        # remote being landed to.
        elsewhere = [
            name for name in _remotes(repo_root)
            if name != remote and _remote_branch_sha(repo_root, name, branch) is not None
        ]
        if elsewhere:
            raise LandRefusal(
                "wrong_remote",
                f"branch {branch!r} is published on {', '.join(elsewhere)} but not on the "
                f"landing remote {remote!r}; landing to a remote the work was never pushed "
                "to would publish content nobody reviewed there",
            )
        raise LandRefusal(
            "branch_unpushed",
            f"branch {branch!r} is not published on {remote!r} (or any other remote); "
            "push it before landing",
        )

    # A surviving worktree still holds work: approval-time cleanup removes one
    # only after proving it clean and fully pushed, so a tree that is still on
    # disk must be re-checked against what the remote actually publishes.
    if Path(worktree).is_dir():
        try:
            dirty = git(worktree, "status", "--porcelain")
            head = git(worktree, "rev-parse", "HEAD")
        except GitError as exc:
            raise LandRefusal("workspace_unusable", f"{task_id}: {exc}") from exc
        if dirty:
            raise LandRefusal(
                "dirty_worktree",
                f"{task_id}: worktree {worktree} has uncommitted changes; the pushed "
                "branch is not the whole change",
            )
        if head != published:
            raise LandRefusal(
                "branch_unpushed",
                f"{branch} is {head[:12]} in {worktree} but {published[:12]} on {remote}; "
                "push the branch before landing (landing never force-pushes)",
            )

    return SourceState(
        task_id=task_id, worktree=worktree, repo_root=repo_root,
        branch=branch, remote=remote, sha=published,
    )
