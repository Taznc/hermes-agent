"""``hermes kanban land`` — attended landing of an explicitly approved review.

Turns a reviewer-approved card into a verified merge to a configured target, a
non-force push, a receipt on the card, closure, and cleanup. Every gate fails
CLOSED: anything unproven refuses with a machine-readable reason code rather
than merging.

Three rules shape the whole module, each answering a way this can go wrong:

* **The target is never inferred.** It comes from ``--target <remote>/<branch>``
  or the board's ``land_target`` and from nowhere else, so an installation whose
  fork and upstream both look plausible cannot be landed to the wrong one.
* **Approval binds to one commit, on the current review cycle.** Verification is
  not review: re-running a test suite says nothing about whether a human read
  the code. A branch that moved after approval, or a newer review cycle nobody
  adjudicated, refuses.
* **The push endpoint is the only endpoint.** Git lets ``remote.<n>.pushurl``
  send writes to a different repository than fetches read from. Every read,
  the push, and the read-back address the resolved push URL, so "we verified
  the thing we wrote" is true by construction rather than by convention.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_approve as kba


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
    approved_sha: str
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
    """The card's LIVE reviewer approval, or :class:`LandRefusal`.

    Approval is the explicit verdict recorded by
    :func:`hermes_cli.kanban_db_approve.approve_review_task` — a run claimed
    from the review column that ended ``approved`` and named the commit it
    approved. Three things are deliberately NOT approval:

    * a ``done`` status (an implementer completing their own card produces
      exactly that, with no reviewer behind it);
    * an approval older than a ``changes_requested`` verdict;
    * an approval older than a newer ``review_requested`` — the card was
      re-submitted, so a cycle is open that nobody has adjudicated. Landing on
      the strength of the previous round would publish whatever the branch
      grew since.
    """
    approval = kba.latest_approval(conn, task_id)
    if approval is not None:
        return ApprovalVerdict(
            run_id=approval.run_id, reviewer=approval.reviewer, summary=approval.summary,
            metadata=approval.metadata, approved_sha=approval.approved_sha,
            approved_at=approval.approved_at,
        )

    # No live approval: say precisely which of the three ways it is missing, so
    # the operator knows whether to re-review, wait, or fix the card.
    newest = conn.execute(
        "SELECT id, outcome FROM task_runs WHERE task_id = ? "
        "AND outcome IN ('approved', 'changes_requested') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if newest is not None and newest["outcome"] == "changes_requested":
        raise LandRefusal(
            "changes_requested_unresolved",
            f"the newest review verdict on {task_id} is 'changes requested' (run "
            f"{newest['id']}); the card must be re-reviewed and approved before landing",
        )
    if newest is not None and newest["outcome"] == "approved":
        raise LandRefusal(
            "approval_superseded",
            f"{task_id} was approved in run {newest['id']}, but a newer review was "
            "requested afterwards; that cycle must be approved on its own before landing",
        )
    raise LandRefusal(
        "no_approval",
        f"{task_id} has no reviewer approval: landing requires an explicit "
        "`hermes kanban approve` verdict from a run claimed out of the review column",
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
class PushEndpoint:
    """The single repository URL a landing reads from, writes to, and re-reads.

    Git treats fetching and pushing as separate configurations: with
    ``remote.origin.pushurl`` set, ``git push origin`` writes somewhere
    ``git ls-remote origin`` never looks. A landing that preflights and reads
    back through the fetch URL while pushing through the push URL verifies a
    repository it did not write and reports success for content that landed in
    another one. Resolving the push URL once and addressing it by URL
    everywhere removes the split entirely.
    """

    remote: str
    url: str


def push_endpoint(repo_root: str, remote: str) -> PushEndpoint:
    """The effective push URL for ``remote``, or :class:`LandRefusal`."""
    try:
        raw = git(repo_root, "remote", "get-url", "--push", "--all", remote, timeout=30)
    except GitError as exc:
        raise LandRefusal(
            "remote_push_disabled",
            f"remote {remote!r} has no usable push URL: {exc}",
        ) from exc
    urls = [u.strip() for u in raw.splitlines() if u.strip()]
    if not urls:
        raise LandRefusal(
            "remote_push_disabled",
            f"remote {remote!r} publishes no push URL, so landing cannot write to it",
        )
    if len(urls) > 1:
        raise LandRefusal(
            "remote_push_ambiguous",
            f"remote {remote!r} has {len(urls)} push URLs ({', '.join(urls)}); landing "
            "refuses to write to several repositories under one name",
        )
    return PushEndpoint(remote=remote, url=urls[0])


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
    still exists."""
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


def _remote_branch_sha(cwd: str, endpoint: str, branch: str) -> Optional[str]:
    """Read-only lookup: the sha ``endpoint`` (a URL or remote name) currently
    publishes for ``branch``, or None. Writes nothing — not even a
    remote-tracking ref."""
    try:
        out = git(cwd, "ls-remote", "--heads", endpoint, f"refs/heads/{branch}", timeout=60)
    except GitError as exc:
        raise LandRefusal(
            "target_unresolvable", f"cannot read remote {endpoint!r}: {exc}",
        ) from exc
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and sha.strip():
            return sha.strip()
    return None


def source_state(
    conn: sqlite3.Connection, task_id: str, *, remote: str,
    endpoint: Optional[PushEndpoint] = None,
) -> SourceState:
    """Everything about the card's own branch, or :class:`LandRefusal`.

    Refuses a live worker, a workspace that is not a usable linked worktree, a
    dirty tree, a branch whose local HEAD is not exactly what the endpoint
    publishes, and an endpoint that does not carry the branch at all. The last
    one is how "you pointed this at the wrong remote" is caught generically:
    the check is that the work is actually THERE, not that the remote has a
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

    # Address the PUSH endpoint, so "the branch is published where we are about
    # to write" is what actually gets checked.
    address = endpoint.url if endpoint is not None else remote
    published = _remote_branch_sha(repo_root, address, branch)
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

    # A surviving worktree still holds work. Approval deliberately preserves it
    # (see ``kanban_db_approve``), so this is the normal case, not the
    # exception: the tree on disk is re-checked against what the remote serves.
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


@contextlib.contextmanager
def fetched_ref(repo_root: str, url: str, branch: str, *, purpose: str):
    """Fetch ``branch`` from ``url`` into a private ref and yield ``(ref, sha)``.

    Two reasons this exists instead of a bare ``ls-remote``:

    * ``ls-remote`` returns a sha the local object database may not HAVE. When
      the target advanced from another clone, every later step (checkout,
      ancestry, merge) fails with "invalid reference" against a perfectly
      healthy remote.
    * the ref name carries a per-run nonce, so two landings — or a landing and
      a reader — can never observe each other's half-written proof ref.
    """
    ref = f"refs/hermes-land/{purpose}/{uuid.uuid4().hex}"
    git(repo_root, "fetch", "--no-tags", url, f"+refs/heads/{branch}:{ref}", timeout=300)
    try:
        yield ref, git(repo_root, "rev-parse", ref, timeout=60)
    finally:
        with contextlib.suppress(Exception):
            git(repo_root, "update-ref", "-d", ref, timeout=60, check=False)


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


def planned_verification(board: Optional[str]) -> dict:
    """What :func:`verify` WOULD do, computed without doing any of it.

    A dry run reports this instead of running the board's command: executing
    arbitrary configured shell is exactly the external state a dry run promises
    not to touch.
    """
    command = str(kb.read_board_metadata(board).get("land_verify") or "").strip()
    if command:
        return {"kind": "command", "planned": True, "command": command}
    return {"kind": "receipt", "planned": True}


def verify(
    conn: sqlite3.Connection, task_id: str, source: SourceState, *,
    board: Optional[str], verdict: Optional[ApprovalVerdict] = None,
) -> dict:
    """Prove the exact ``source.sha`` was verified, or refuse.

    Two accepted sources, in order:

    1. The board's ``land_verify`` command, re-run now against a fresh detached
       checkout of the exact sha being landed.
    2. Otherwise a verification receipt on the approval run's metadata, which
       must name the sha being landed.

    Neither is a substitute for review — the caller has already bound
    ``source.sha`` to the reviewed commit. This only answers "and does that
    exact commit pass?". There is deliberately no override flag: an
    unverifiable card is a card the operator must fix, not one they can wave
    through.
    """
    command = str(kb.read_board_metadata(board).get("land_verify") or "").strip()
    if command:
        import subprocess

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

    verdict = verdict or approval_verdict(conn, task_id)
    sha, receipt, origin = _find_receipt(conn, task_id, verdict)
    if receipt is None:
        raise LandRefusal(
            "verification_missing",
            f"{task_id} carries no verification evidence: configure a board land_verify "
            f"command, or record a {'/'.join(_RECEIPT_KEYS)} receipt on the approval or "
            "review-handoff run",
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
            "receipt_run_id": origin, "approval_run_id": verdict.run_id}


def _find_receipt(
    conn: sqlite3.Connection, task_id: str, verdict: ApprovalVerdict,
) -> tuple[Optional[str], Optional[dict], Optional[int]]:
    """``(sha, receipt, run_id)`` — the verification receipt for this card.

    Looked for on the approval run first, then on the runs that handed the card
    to review. The second is where it actually lives: the pre-review gate is run
    and recorded by the IMPLEMENTER on the review handoff, and a reviewer
    approving the card does not retype it. Reading only the approval run would
    make every real card refuse ``verification_missing``.

    Widening WHERE the receipt may live does not widen WHAT it proves: the
    caller still requires it to name the exact commit being landed, and that
    commit is already pinned to the reviewed one by ``approval_sha_drift``. A
    receipt from an earlier round therefore names an older sha and is refused
    as stale.
    """
    sha, receipt = _receipt_sha(verdict.metadata)
    if receipt is not None:
        return sha, receipt, verdict.run_id
    for row in conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ? "
        "AND outcome = 'review_requested' ORDER BY id DESC", (task_id,),
    ).fetchall():
        sha, receipt = _receipt_sha(_json_dict(row["metadata"]))
        if receipt is not None:
            return sha, receipt, int(row["id"])
    return None, None, None


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


def _adds_nothing_to(cwd: str, target_ref: str, source_sha: str) -> bool:
    """Whether merging ``source_sha`` into ``target_ref`` would change nothing.

    This is the squash-equivalence test, and it deliberately asks about the
    target's CURRENT TREE rather than about its history. ``git cherry`` answers
    "did an equivalent patch ever appear upstream", which is a different and
    much weaker question: work that was applied and then reverted still has an
    equivalent patch in history while being absent from the tip, so cherry
    reports it landed when the file is gone. It also misses the ordinary case
    it exists for, because an N-commit branch squashed into one commit has no
    per-commit patch id in common with its own squash.

    Merging into the tip and comparing trees answers the question that actually
    matters — "is the reviewed content already present, right now?" — and gets
    the 1-commit squash, the N-commit squash, and the applied-then-reverted
    case all correct for the same reason.
    """
    try:
        merged_tree = git(cwd, "merge-tree", "--write-tree", target_ref, source_sha, timeout=300)
        target_tree = git(cwd, "rev-parse", f"{target_ref}^{{tree}}", timeout=60)
    except GitError:
        # A conflicting merge cannot be "already present"; the real merge below
        # will report the conflict properly.
        return False
    return bool(merged_tree) and merged_tree.splitlines()[0].strip() == target_tree


def _landed_state(cwd: str, target_ref: str, source_sha: str) -> Optional[str]:
    """``"ancestor"`` / ``"patch_equivalent"`` when the work is already on the
    target, else None. This is what makes a re-run idempotent."""
    if _is_ancestor(cwd, source_sha, target_ref):
        return "ancestor"
    if _adds_nothing_to(cwd, target_ref, source_sha):
        return "patch_equivalent"
    return None


def land_task(
    conn: sqlite3.Connection, task_id: str, *, target: tuple[str, str],
    dry_run: bool = False, board: Optional[str] = None, actor: str = "kanban land",
) -> dict:
    """Land one approved card onto ``target``; raises :class:`LandRefusal`.

    Order matters and is the safety model:

    1. Gate on the board's own record (live approval, no live worker, deps).
    2. Resolve the single push endpoint every later step addresses.
    3. Gate on git (clean, pushed, right remote) and on the approved commit.
    4. A dry run stops here, having mutated nothing at all — no fetch, no
       staging tree, and no configured verification command.
    5. Gate on verification evidence for that exact sha.
    6. Fetch the target NOW, merge in a throwaway worktree, push without force.
    7. Re-read the endpoint and prove the content is reachable there.
    8. Only then write the receipt, close the card, and let cleanup run.
    """
    remote, branch = target
    verdict = approval_verdict(conn, task_id)

    workspace_row = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    repo_hint = _repo_root_for(str(workspace_row["workspace_path"] or "")) if workspace_row else None
    endpoint = push_endpoint(repo_hint, remote) if repo_hint else None

    source = source_state(conn, task_id, remote=remote, endpoint=endpoint)
    if endpoint is None:
        endpoint = push_endpoint(source.repo_root, remote)

    if not _same_commit(verdict.approved_sha, source.sha):
        raise LandRefusal(
            "approval_sha_drift",
            f"{task_id} was approved at {verdict.approved_sha[:12]} but {source.branch} now "
            f"publishes {source.sha[:12]}; the difference was never reviewed. Re-request "
            "review for the new commit — a passing verification is not a review.",
        )

    target_head = _remote_branch_sha(source.repo_root, endpoint.url, branch)
    if target_head is None:
        raise LandRefusal(
            "target_unresolvable",
            f"remote {remote!r} does not publish branch {branch!r}; landing refuses to "
            "create a target branch it was not told exists",
        )

    base: dict = {
        "task_id": task_id, "remote": remote, "branch": branch,
        "target": f"{remote}/{branch}", "push_url": endpoint.url,
        "source_branch": source.branch, "source_sha": source.sha,
        "target_sha_before": target_head, "reviewer": verdict.reviewer,
        "approval_run_id": verdict.run_id, "approved_sha": verdict.approved_sha,
        "approved_at": verdict.approved_at, "dry_run": bool(dry_run),
    }

    if dry_run:
        # Zero mutation, and that includes the object database and the
        # operator's configured shell: no fetch, no staging worktree, no ref
        # write, no verification command, no board write. Idempotency is
        # reported only from objects already present locally.
        already = "ancestor" if _is_ancestor(source.repo_root, source.sha, target_head) else None
        return {
            **base,
            "verdict": "already_landed" if already else "would_land",
            "verification": planned_verification(board),
            "readback": already, "target_sha": target_head,
            "pushed": False, "reason": None,
        }

    receipt = verify(conn, task_id, source, board=board, verdict=verdict)

    with fetched_ref(source.repo_root, endpoint.url, branch, purpose="target") as (
        target_ref, fetched_head,
    ):
        with staged_checkout(source.repo_root, target_ref) as tree:
            already = _landed_state(tree, target_ref, source.sha)
            if already is None:
                _merge_in(tree, source, remote, branch)
                try:
                    git(tree, "push", endpoint.url, f"HEAD:refs/heads/{branch}", timeout=300)
                except GitError as exc:
                    raise LandRefusal(
                        _push_refusal_reason(str(exc)),
                        f"pushing {source.branch} to {remote}/{branch} was rejected "
                        f"(landing never force-pushes): {exc}",
                    ) from exc

    # Read-back: ask the ENDPOINT WE WROTE TO what it now publishes, then prove
    # the reviewed content is reachable from it. A push that reported success is
    # not evidence; what the remote serves afterwards is.
    landed_sha = _remote_branch_sha(source.repo_root, endpoint.url, branch)
    if landed_sha is None:
        raise LandRefusal(
            "readback_failed",
            f"{remote}/{branch} no longer resolves after the push; refusing to close "
            f"{task_id} without proof the content landed",
        )
    with fetched_ref(source.repo_root, endpoint.url, branch, purpose="readback") as (
        readback_ref, readback_sha,
    ):
        # The proof and the recorded sha must be the same object: a target that
        # moved between the ls-remote and the fetch would otherwise let us
        # verify one commit and record another.
        if readback_sha != landed_sha:
            raise LandRefusal(
                "readback_failed",
                f"{remote}/{branch} moved during read-back ({landed_sha[:12]} -> "
                f"{readback_sha[:12]}); {task_id} stays open rather than record a proof "
                "of a commit that was not the one verified",
            )
        state = _landed_state(source.repo_root, readback_ref, source.sha)
        if state is None:
            raise LandRefusal(
                "readback_failed",
                f"{source.sha[:12]} is neither reachable from nor already present in "
                f"{remote}/{branch} at {readback_sha[:12]} after the push; {task_id} stays open",
            )

    result = {
        **base,
        "verdict": "already_landed" if already else "landed",
        "verification": receipt,
        "readback": state, "readback_sha": readback_sha,
        "target_sha": readback_sha, "pushed": already is None,
        "landed_at": int(time.time()), "reason": None,
        "closure_reason": (
            f"content proven present on {remote}/{branch} at {readback_sha[:12]} "
            f"({state}) after remote read-back"
        ),
    }
    result["cleanup"] = _record_landing(conn, task_id, result, actor=actor)
    return result


_NON_FAST_FORWARD_MARKERS = ("non-fast-forward", "fetch first", "stale info")


def _push_refusal_reason(stderr: str) -> str:
    """``target_advanced`` when the remote moved under us, else ``push_rejected``.

    Both are refusals, but they mean different things to the operator: one says
    "re-run me", the other says "your branch protection said no".
    """
    lowered = stderr.lower()
    return (
        "target_advanced"
        if any(marker in lowered for marker in _NON_FAST_FORWARD_MARKERS)
        else "push_rejected"
    )


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


def _record_landing(
    conn: sqlite3.Connection, task_id: str, result: dict, *, actor: str,
) -> dict:
    """Write the durable landing record, then close and clean up the card.

    The receipt is written BEFORE closure so a crash between the two leaves
    evidence of what was landed rather than a silently-merged, still-open card.
    Returns what cleanup actually achieved, so the receipt can say so rather
    than assume it.
    """
    kb.add_comment(conn, task_id, actor, _receipt_body(result))
    workspace = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    workspace_path = str((workspace["workspace_path"] if workspace else "") or "")

    task = kb.get_task(conn, task_id)
    if task is not None and task.status != "done":
        # Completion is what invokes the existing safe cleanup seam
        # (``_cleanup_workspace``), which independently re-proves the tree is
        # clean and fully pushed before removing anything.
        kb.complete_task(
            conn, task_id,
            summary=f"Landed on {result['target']} as {result['target_sha'][:12]}",
            metadata={"landing": result},
        )
    kb.archive_task(conn, task_id)
    return {
        "workspace_path": workspace_path or None,
        "workspace_removed": bool(workspace_path) and not Path(workspace_path).exists(),
        "card_archived": True,
    }


def _receipt_body(result: dict) -> str:
    verification = result.get("verification") or {}
    landed_at = result.get("landed_at")
    stamp = (
        time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(landed_at)) if landed_at else "unknown"
    )
    return "\n".join([
        f"**Landed on `{result['target']}`** ({result['verdict']})",
        "",
        f"- Source: `{result['source_branch']}` @ `{result['source_sha']}`",
        f"- Remote: `{result['remote']}`  Branch: `{result['branch']}`",
        f"- Push endpoint: `{result.get('push_url')}`",
        f"- Target before: `{result['target_sha_before']}`",
        f"- Target after (read back from the remote): `{result.get('readback_sha')}`",
        f"- Pushed: {'yes' if result['pushed'] else 'no (already present)'}",
        f"- Remote read-back: {result['readback']}",
        f"- Reviewer verdict: approved by {result.get('reviewer') or 'unknown'} "
        f"(run {result.get('approval_run_id')}) at `{result.get('approved_sha')}`",
        f"- Verification: {verification.get('kind')} "
        f"({verification.get('command') or verification.get('sha', '')})",
        f"- Landed at: {stamp}",
        f"- Closure: {result.get('closure_reason')}; card completed and archived.",
    ])


# ---------------------------------------------------------------------------
# CLI handler — batch mode isolates every task from its siblings
# ---------------------------------------------------------------------------


def _cmd_land(args) -> int:
    """``hermes kanban land <task-id...> [--target R/B] [--dry-run] [--json]``."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_output import _err, _json_out

    task_ids = list(dict.fromkeys(args.task_ids or []))
    if not task_ids:
        return _err("kanban land: at least one task_id is required", 2)

    board = getattr(args, "board", None)
    dry_run = bool(getattr(args, "dry_run", False))
    actor = _actor()

    # Resolved ONCE, outside the per-task loop, so that every refusal record —
    # including one from a board-configured default — can name the remote and
    # branch it would have landed to.
    target: Optional[tuple[str, str]] = None
    target_refusal: Optional[LandRefusal] = None
    try:
        target = resolve_target(getattr(args, "target", None), board=board)
    except LandRefusal as exc:
        target_refusal = exc

    results: list[dict] = []
    with kbc.connect_closing() as conn:
        for task_id in task_ids:
            # Per-task isolation: a refusal or an unexpected git/DB failure on
            # one card is recorded as that card's verdict and never aborts the
            # batch or contaminates another card's report.
            if target_refusal is not None:
                results.append(_refusal_record(
                    task_id, target, target_refusal.reason, target_refusal.message, dry_run,
                ))
                continue
            assert target is not None
            try:
                results.append(
                    land_task(conn, task_id, target=target, dry_run=dry_run,
                              board=board, actor=actor),
                )
            except LandRefusal as exc:
                results.append(_refusal_record(task_id, target, exc.reason, exc.message, dry_run))
            except (GitError, OSError, RuntimeError, ValueError) as exc:
                results.append(_refusal_record(task_id, target, "error", str(exc), dry_run))

    refused = [r for r in results if r["verdict"] == "refused"]
    if not _json_out(args, results):
        for record in results:
            print(_land_line(record))
    return 1 if refused else 0


def _refusal_record(
    task_id: str, target: Optional[tuple[str, str]], reason: str, message: str, dry_run: bool,
) -> dict:
    """A refusal reported in the same shape as a success, so a batch report is
    uniform and machine-readable. The RESOLVED target is carried in, so a
    board-configured default is named in the output exactly like an explicit
    ``--target`` would be."""
    remote, branch = target if target else (None, None)
    return {
        "task_id": task_id, "verdict": "refused", "reason": reason, "message": message,
        "remote": remote, "branch": branch,
        "target": f"{remote}/{branch}" if target else None,
        "dry_run": dry_run, "pushed": False,
        "source_sha": None, "target_sha": None, "readback": None,
    }


def _land_line(record: dict) -> str:
    """One human line per task. Always names the remote and branch."""
    target = record.get("target") or "(no target configured)"
    head = f"{record['task_id']}  {record['verdict']}  → {target}"
    if record["verdict"] == "refused":
        return f"✗ {head}\n    {record['reason']}: {record['message']}"
    detail = (
        f"    source {(record.get('source_sha') or '')[:12]} "
        f"→ target {(record.get('target_sha') or '')[:12]} "
        f"({'pushed' if record.get('pushed') else 'no push needed'}; "
        f"read-back: {record.get('readback') or 'n/a'})"
    )
    return f"{'…' if record.get('dry_run') else '✓'} {head}\n{detail}"


def _actor() -> str:
    """Author recorded on the landing receipt comment."""
    import os

    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = os.environ.get(env)
        if value:
            return f"kanban land ({value})"
    return "kanban land"
