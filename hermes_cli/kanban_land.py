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

import contextlib
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


@contextlib.contextmanager
def staged_checkout(repo_root: str, ref: str):
    """A throwaway DETACHED worktree at ``ref``, removed on exit.

    Landing never touches the operator's own checkout or any served worktree:
    every merge, test run, and inspection happens in a temporary tree, so a
    failure mid-landing cannot leave a real working tree on a foreign branch.
    Detached HEAD also means the staging tree can never occupy a branch another
    worktree needs.
    """
    import shutil
    import tempfile

    staging = tempfile.mkdtemp(prefix="hermes-land-")
    tree = str(Path(staging) / "tree")
    try:
        git(repo_root, "worktree", "add", "--detach", tree, ref, timeout=300)
        yield tree
    finally:
        with contextlib.suppress(Exception):
            git(repo_root, "worktree", "remove", "--force", tree, timeout=120, check=False)
        shutil.rmtree(staging, ignore_errors=True)
        with contextlib.suppress(Exception):
            git(repo_root, "worktree", "prune", timeout=60, check=False)


# ---------------------------------------------------------------------------
# Verification contract — evidence that the exact source sha was tested
# ---------------------------------------------------------------------------

# Keys a reviewer/implementer handoff may use to record a verification receipt,
# and the keys inside one that may carry the sha it vouches for.
_RECEIPT_KEYS = ("pre_review_gate", "verification")
_RECEIPT_SHA_KEYS = ("pushed", "sha", "commit", "head")


def _receipt_sha(metadata: dict) -> tuple[Optional[str], Optional[dict]]:
    """``(sha, receipt)`` from an approval run's metadata; ``(None, None)`` when
    no recognised receipt is present."""
    for key in _RECEIPT_KEYS:
        receipt = metadata.get(key)
        if not isinstance(receipt, dict):
            continue
        for sha_key in _RECEIPT_SHA_KEYS:
            value = receipt.get(sha_key)
            if isinstance(value, str) and value.strip():
                return value.strip(), receipt
        return None, receipt
    return None, None


def verify(
    conn: sqlite3.Connection, task_id: str, source: SourceState, *, board: Optional[str],
) -> dict:
    """Prove the exact ``source.sha`` was verified, or refuse.

    Two accepted sources, in order:

    1. The board's ``land_verify`` command, re-run now in the task worktree.
       This is the strongest evidence and is preferred whenever configured.
    2. Otherwise a verification receipt on the approval run's metadata, which
       must name the sha being landed.

    There is deliberately no override flag: an unverifiable card is a card the
    operator must fix, not one they can wave through.
    """
    command = str(kb.read_board_metadata(board).get("land_verify") or "").strip()
    if command:
        import subprocess

        # Run against a fresh detached checkout of the exact sha being landed,
        # not the task worktree: approval-time cleanup reaps that tree, and a
        # surviving one could have drifted since it was reviewed.
        with staged_checkout(source.repo_root, source.sha) as tree:
            proc = subprocess.run(
                command, shell=True, cwd=tree, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=3600,
            )
        if proc.returncode != 0:
            raise LandRefusal(
                "verification_failed",
                f"board verification command exited {proc.returncode} for {task_id}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}",
            )
        return {
            "kind": "command", "ok": True, "command": command,
            "sha": source.sha, "exit_code": 0,
        }

    verdict = approval_verdict(conn, task_id)
    sha, receipt = _receipt_sha(verdict.metadata)
    if receipt is None:
        raise LandRefusal(
            "verification_missing",
            f"{task_id} carries no verification evidence: configure a board land_verify "
            f"command, or record a {'/'.join(_RECEIPT_KEYS)} receipt on the approval run",
        )
    if sha is None:
        raise LandRefusal(
            "verification_missing",
            f"{task_id}'s verification receipt names no commit "
            f"({'/'.join(_RECEIPT_SHA_KEYS)}), so it cannot vouch for {source.sha[:12]}",
        )
    if not _same_commit(sha, source.sha):
        raise LandRefusal(
            "verification_stale",
            f"{task_id}'s verification receipt vouches for {sha[:12]} but "
            f"{source.sha[:12]} would be landed",
        )
    return {"kind": "receipt", "ok": True, "sha": source.sha, "receipt": receipt,
            "approval_run_id": verdict.run_id}


def _same_commit(a: str, b: str) -> bool:
    """Compare two commit ids, tolerating an abbreviated receipt sha."""
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


# ---------------------------------------------------------------------------
# Landing — merge, push, read back, then (and only then) close and clean up
# ---------------------------------------------------------------------------


def _is_ancestor(cwd: str, ancestor: str, descendant: str) -> bool:
    """Whether ``ancestor`` is reachable from ``descendant``."""
    try:
        git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, timeout=60)
        return True
    except GitError:
        return False


def _patch_equivalent(cwd: str, upstream: str, head: str) -> bool:
    """Whether every commit unique to ``head`` already exists on ``upstream`` as
    an equivalent patch — how a squash-merged branch reads afterwards.

    ``git cherry`` marks a commit ``-`` when an equivalent patch is upstream and
    ``+`` when it is not, so "no ``+`` lines, at least one commit examined" is
    exactly "this work is already there in some form".
    """
    try:
        out = git(cwd, "cherry", upstream, head, timeout=120)
    except GitError:
        return False
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return bool(lines) and not any(ln.startswith("+") for ln in lines)


def _landed_state(cwd: str, target_ref: str, source_sha: str) -> Optional[str]:
    """``"ancestor"`` / ``"patch_equivalent"`` when the work is already on the
    target, else None. This is what makes a re-run idempotent."""
    if _is_ancestor(cwd, source_sha, target_ref):
        return "ancestor"
    if _patch_equivalent(cwd, target_ref, source_sha):
        return "patch_equivalent"
    return None


def land_task(
    conn: sqlite3.Connection, task_id: str, *, target: tuple[str, str],
    dry_run: bool = False, board: Optional[str] = None, actor: str = "kanban land",
) -> dict:
    """Land one approved card onto ``target``; raises :class:`LandRefusal`.

    Order matters and is the safety model:

    1. Gate on the board's own record (approval, no live worker, deps).
    2. Gate on git (clean, pushed, right remote).
    3. Gate on verification evidence for the exact sha.
    4. Re-read the target from the remote NOW, merge in a throwaway worktree,
       push without force.
    5. Re-read the remote again and prove the content is reachable there.
    6. Only then write the receipt, close the card, and let cleanup run.

    Nothing before step 4 mutates anything, and step 6 never runs without a
    successful step 5 read-back.
    """
    remote, branch = target
    verdict = approval_verdict(conn, task_id)
    source = source_state(conn, task_id, remote=remote)
    receipt = verify(conn, task_id, source, board=board)

    target_head = _remote_branch_sha(source.repo_root, remote, branch)
    if target_head is None:
        raise LandRefusal(
            "target_unresolvable",
            f"remote {remote!r} does not publish branch {branch!r}; landing refuses to "
            "create a target branch it was not told exists",
        )

    base: dict = {
        "task_id": task_id, "remote": remote, "branch": branch,
        "target": f"{remote}/{branch}", "source_branch": source.branch,
        "source_sha": source.sha, "target_sha_before": target_head,
        "reviewer": verdict.reviewer, "approval_run_id": verdict.run_id,
        "verification": receipt, "dry_run": bool(dry_run),
    }

    with staged_checkout(source.repo_root, target_head) as tree:
        already = _landed_state(tree, target_head, source.sha)
        if dry_run:
            return {
                **base,
                "verdict": "already_landed" if already else "would_land",
                "readback": already,
                "target_sha": target_head,
                "pushed": False,
                "reason": None,
            }
        if already is None:
            _merge_in(tree, source, remote, branch)
            try:
                git(tree, "push", remote, f"HEAD:refs/heads/{branch}", timeout=300)
            except GitError as exc:
                raise LandRefusal(
                    "push_rejected",
                    f"pushing {source.branch} to {remote}/{branch} was rejected "
                    f"(landing never force-pushes): {exc}",
                ) from exc

        # Read-back: ask the REMOTE what it now publishes, then prove the
        # reviewed content is reachable from it. A push that reported success
        # is not evidence; what the remote serves afterwards is.
        landed_sha = _remote_branch_sha(source.repo_root, remote, branch)
        if landed_sha is None:
            raise LandRefusal(
                "readback_failed",
                f"{remote}/{branch} no longer resolves after the push; refusing to close "
                f"{task_id} without proof the content landed",
            )
        git(tree, "fetch", remote, f"+refs/heads/{branch}:refs/land-readback", timeout=300)
        state = _landed_state(tree, "refs/land-readback", source.sha)
        if state is None:
            raise LandRefusal(
                "readback_failed",
                f"{source.sha[:12]} is neither reachable from nor patch-equivalent to "
                f"{remote}/{branch} at {landed_sha[:12]} after the push; {task_id} stays open",
            )

    result = {
        **base,
        "verdict": "already_landed" if already else "landed",
        "readback": state,
        "target_sha": landed_sha,
        "pushed": already is None,
        "reason": None,
    }
    _record_landing(conn, task_id, result, actor=actor)
    return result


def _merge_in(tree: str, source: SourceState, remote: str, branch: str) -> None:
    """Merge the source sha into the staged target checkout, or refuse.

    ``--no-ff`` keeps the card's work identifiable as one merge on the target,
    and the abort leaves no half-merged state behind (the tree is thrown away
    anyway, but an aborted merge keeps the error message clean).
    """
    try:
        git(
            tree, "merge", "--no-ff", "--no-edit",
            "-m", f"Merge {source.branch} into {branch} ({source.task_id})",
            source.sha, timeout=300,
        )
    except GitError as exc:
        with contextlib.suppress(Exception):
            git(tree, "merge", "--abort", timeout=60, check=False)
        raise LandRefusal(
            "merge_conflict",
            f"{source.branch} does not merge cleanly into {remote}/{branch}: {exc}. "
            "Merge the target into the card branch, re-verify, and land again.",
        ) from exc


def _record_landing(conn: sqlite3.Connection, task_id: str, result: dict, *, actor: str) -> None:
    """Write the durable landing record, then close and clean up the card.

    The receipt is written BEFORE closure so a crash between the two leaves
    evidence of what was landed rather than a silently-merged, still-open card.
    """
    kb.add_comment(conn, task_id, actor, _receipt_body(result))
    if kb.get_task(conn, task_id).status != "done":
        kb.complete_task(
            conn, task_id, summary=f"Landed on {result['target']} as {result['target_sha'][:12]}",
            metadata={"landing": result},
        )
    # Archival triggers the existing safe worktree/branch cleanup seam
    # (``_cleanup_workspace``), which independently re-proves the tree is clean
    # and fully pushed before removing anything.
    kb.archive_task(conn, task_id)


def _receipt_body(result: dict) -> str:
    verification = result.get("verification") or {}
    return "\n".join([
        f"**Landed on `{result['target']}`** ({result['verdict']})",
        "",
        f"- Source: `{result['source_branch']}` @ `{result['source_sha']}`",
        f"- Remote: `{result['remote']}`  Branch: `{result['branch']}`",
        f"- Target before: `{result['target_sha_before']}`",
        f"- Target after: `{result['target_sha']}`",
        f"- Pushed: {'yes' if result['pushed'] else 'no (already present)'}",
        f"- Remote read-back: {result['readback']}",
        f"- Reviewer: {result.get('reviewer') or 'unknown'} "
        f"(approval run {result.get('approval_run_id')})",
        f"- Verification: {verification.get('kind')} "
        f"({verification.get('command') or verification.get('sha', '')})",
        f"- Closure: content proven reachable on {result['target']}; card completed and archived.",
    ])
