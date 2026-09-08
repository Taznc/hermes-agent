"""Kanban worker preservation safety net: commit + push a task worktree's
recoverable work when its run ends.

This is a PRESERVATION safety net, not merge automation. It only ever does two
things — create one commit on the branch the worktree is already on, and push
that branch to its configured remote without force. It never merges, rebases,
force-pushes, switches branches, deletes a worktree or branch, or touches any
other task's workspace.

Every ambiguity fails CLOSED (no commit, an actionable reason) so a dirty
worktree is preserved for a human rather than snapshotted blindly.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

_GIT_TIMEOUT = 60

# One fixed subject so a preservation commit is recognizable in history and a
# human can grep for it. The body carries the task id.
_COMMIT_SUBJECT = "chore(kanban): preserve in-progress worker output"

# Content-safety budgets. A safety net snapshot is meant to rescue source-sized
# work; anything larger is a build artifact or a dataset a human should place
# deliberately, so it fails closed rather than landing in branch history.
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024

# Path components that mark generated/vendored trees. Gitignored files never
# reach this check (git excludes them from ``status``); this catches the tree
# whose .gitignore simply forgot them.
_GENERATED_DIR_NAMES = frozenset({
    "node_modules", "dist", "build", "target", "__pycache__", "site-packages",
    ".venv", "venv", ".next", ".nuxt", ".tox", ".mypy_cache", ".pytest_cache",
    ".gradle", "vendor", "coverage", ".terraform",
})

# Filenames that carry credentials by convention. Their CONTENTS are never the
# question — a ``.env`` full of harmless ports is still a file no automation
# should commit on a human's behalf.
_SECRET_FILE_NAMES = frozenset({
    ".env", ".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc",
    "credentials", "credentials.json", "client_secret.json", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity",
    ".dockercfg", "kubeconfig",
})
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk")
_SECRET_FILE_PREFIXES = (".env.", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")

# Files larger than this are not content-scanned for secrets (they are already
# refused by the size guard, and reading them would be the expensive path).
_SCAN_READ_LIMIT = 1 * 1024 * 1024


@dataclass
class PreserveResult:
    """Outcome of one preservation attempt. ``status`` is the machine-readable
    verdict; ``reason`` explains every non-``preserved`` status."""

    status: str
    reason: Optional[str] = None
    commit_sha: Optional[str] = None
    pushed: Optional[bool] = None
    push_error: Optional[str] = None
    branch: Optional[str] = None
    detail: Optional[str] = None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """``git -C cwd args``; never raises on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT,
        check=False,
    )


def _git_out(cwd: Path, *args: str) -> Optional[str]:
    result = _git(cwd, *args)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _has_unpushed_commits(worktree: Path) -> bool:
    """True when HEAD carries commits unreachable from every remote-tracking ref.

    Deliberately the same predicate ``_cleanup_worktree_workspace`` uses to
    refuse removal, so a successful preservation is exactly what makes cleanup
    safe. Fails SAFE toward True: unknown state means there may be work.
    """
    remote_refs = _git(worktree, "for-each-ref", "--format=%(refname)", "refs/remotes")
    if remote_refs.returncode != 0:
        return True
    if not (remote_refs.stdout or "").strip():
        return False  # no remote-tracking refs: nothing to be unpushed against
    unpushed = _git(worktree, "log", "--oneline", "HEAD", "--not", "--remotes")
    if unpushed.returncode != 0:
        return True
    return bool((unpushed.stdout or "").strip())


def _dirty_paths(worktree: Path) -> Optional[list[str]]:
    """Paths git reports as changed (gitignored files are excluded by git
    itself), or ``None`` when the status probe failed."""
    result = _git(worktree, "status", "--porcelain", "-z")
    if result.returncode != 0:
        return None
    entries = [e for e in (result.stdout or "").split("\0") if e]
    paths: list[str] = []
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        code, _, rest = entry.partition(" ")
        # Rename/copy entries are followed by their source path record.
        if code and code[0] in {"R", "C"}:
            skip_next = True
        paths.append(rest.strip() or entry[3:].strip())
    return [p for p in paths if p]


def _expand_candidates(worktree: Path, paths: list[str]) -> list[str]:
    """Expand git's directory-collapsed untracked entries (``dir/``) into the
    real files underneath, so a guard can never be evaded by collapsing."""
    expanded: list[str] = []
    for path in paths:
        if not path.endswith("/"):
            expanded.append(path)
            continue
        base = worktree / path
        if not base.is_dir():
            expanded.append(path.rstrip("/"))
            continue
        for child in sorted(base.rglob("*")):
            if child.is_file() and not child.is_symlink():
                expanded.append(child.relative_to(worktree).as_posix())
    return expanded


def _generated_component(path: str) -> Optional[str]:
    """The first generated/vendored path component of *path*, if any."""
    for part in Path(path).parts:
        if part in _GENERATED_DIR_NAMES:
            return part
    return None


def _looks_like_secret_filename(path: str) -> bool:
    name = Path(path).name
    lowered = name.lower()
    if lowered in _SECRET_FILE_NAMES:
        return True
    if lowered.endswith(_SECRET_FILE_SUFFIXES):
        return True
    return any(
        lowered.startswith(prefix) and lowered != prefix.rstrip(".")
        for prefix in _SECRET_FILE_PREFIXES
    )


def _contains_credential(file_path: Path) -> bool:
    """True when the redactor finds a credential in *file_path*'s text.

    Reuses ``agent.redact`` rather than a second pattern list so the
    preservation guard and every other Hermes safety boundary agree on what a
    credential looks like. ``code_file=True`` keeps ordinary source constants
    (``MAX_TOKENS=...``, test fixtures) from tripping it; prefix-matched real
    credentials, private-key blocks and JWTs still do.
    """
    try:
        if file_path.stat().st_size > _SCAN_READ_LIMIT:
            return False  # size guard already refuses these
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False  # binary/unreadable: the size + artifact guards own these
    try:
        from agent.redact import redact_sensitive_text
    except Exception:
        return False
    return redact_sensitive_text(text, force=True, code_file=True) != text


def _check_content_safety(
    worktree: Path, paths: list[str], *,
    max_file_bytes: int, max_total_bytes: int,
) -> Optional[PreserveResult]:
    """``None`` when every candidate is safe to commit, else the fail-closed
    ``unsafe`` verdict naming the offending path (never its contents)."""
    total = 0
    for rel in _expand_candidates(worktree, paths):
        generated = _generated_component(rel)
        if generated is not None:
            return PreserveResult(
                status="unsafe", reason="generated_artifact",
                detail=f"{rel} is inside a generated/vendored directory ({generated}/)",
            )
        if _looks_like_secret_filename(rel):
            return PreserveResult(
                status="unsafe", reason="suspected_secret",
                detail=f"{rel} has a credential-bearing filename",
            )
        full = worktree / rel
        try:
            size = full.stat().st_size if full.is_file() else 0
        except OSError:
            size = 0
        if size > max_file_bytes:
            return PreserveResult(
                status="unsafe", reason="oversized",
                detail=f"{rel} is {size} bytes (limit {max_file_bytes})",
            )
        total += size
        if total > max_total_bytes:
            return PreserveResult(
                status="unsafe", reason="oversized",
                detail=f"snapshot exceeds {max_total_bytes} bytes at {rel}",
            )
        if full.is_file() and not full.is_symlink() and _contains_credential(full):
            return PreserveResult(
                status="unsafe", reason="suspected_secret",
                detail=f"{rel} contains what looks like a credential",
            )
    return None


def _is_git_worktree(path: Path) -> bool:
    """True when *path* is inside a git working tree (and not a bare repo)."""
    out = _git_out(path, "rev-parse", "--is-inside-work-tree")
    return out == "true"


def _resolve_push_remote(worktree: Path, branch: str) -> Optional[str]:
    """The remote this branch should be pushed to, or ``None`` when the repo
    has none configured.

    Precedence: the branch's own ``branch.<name>.remote`` (a worker may have
    set an upstream), then ``origin``, then the sole remote when exactly one
    exists. Several remotes with no branch config and no ``origin`` is
    ambiguous, so it resolves to ``None`` and preservation stays local rather
    than guessing which host to publish work to.
    """
    configured = _git_out(worktree, "config", "--get", f"branch.{branch}.remote")
    remotes = (_git_out(worktree, "remote") or "").split()
    if configured and configured in remotes:
        return configured
    if "origin" in remotes:
        return "origin"
    return remotes[0] if len(remotes) == 1 else None


@contextlib.contextmanager
def _preserve_lock(worktree: Path, task_id: Optional[str]) -> Iterator[bool]:
    """Hold the per-worktree preservation lock, yielding whether it was taken.

    Two lifecycle paths can fire on one worktree at the same instant (a worker
    completing while the dispatcher reclaims its stale run). Without exclusion
    both run ``git add``/``git commit`` against the SAME index and produce
    either two snapshot commits or a corrupt index; the loser must stand down.

    The lock file lives in the worktree's own git dir, so it is per-worktree
    (never shared across tasks the way ``refs/stash`` is) and disappears with
    the worktree. Non-blocking: a busy lock means another preserver already
    owns this work, and waiting for it would only duplicate the outcome.
    ``fcntl`` is POSIX-only; where it is unavailable the lock degrades to
    "always acquired", matching the pre-existing single-preserver behaviour on
    that platform rather than blocking preservation entirely.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield True
        return
    git_dir = _git_out(worktree, "rev-parse", "--path-format=absolute", "--git-dir")
    if not git_dir:
        yield True
        return
    name = f"hermes-kanban-preserve-{task_id or 'worktree'}.lock"
    lock_path = Path(git_dir) / name
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        yield True
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def preserve_worktree(
    worktree: Path, branch: str, *,
    task_id: Optional[str] = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> PreserveResult:
    """Commit the worktree's dirty changes on ``branch`` and push it."""
    worktree = Path(worktree)
    if not _is_git_worktree(worktree):
        return PreserveResult(status="skipped", reason="not_a_git_worktree")
    with _preserve_lock(worktree, task_id) as acquired:
        if not acquired:
            return PreserveResult(status="skipped", reason="concurrent")
        return _preserve_locked(
            worktree, branch,
            max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
        )


def _preserve_locked(
    worktree: Path, branch: str, *, max_file_bytes: int, max_total_bytes: int,
) -> PreserveResult:
    """The preservation body; the caller holds the per-worktree lock."""
    current = _git_out(worktree, "branch", "--show-current")
    # Fail closed on any branch ambiguity: a detached HEAD has no branch to
    # preserve onto, and a different branch means our ownership belief about
    # this worktree is wrong. Neither is ever "fixed" by checking out.
    if not current:
        return PreserveResult(status="skipped", reason="detached_head")
    if branch and current != branch:
        return PreserveResult(
            status="skipped", reason="branch_mismatch", branch=current,
            detail=f"worktree is on {current!r}, expected {branch!r}",
        )
    dirty = _dirty_paths(worktree)
    if dirty is None:
        return PreserveResult(status="failed", reason="git_status_failed", branch=current)
    unpushed = _has_unpushed_commits(worktree)
    if not dirty and not unpushed:
        return PreserveResult(status="nothing_to_preserve", branch=current)

    sha = _git_out(worktree, "rev-parse", "HEAD")
    if dirty:
        unsafe = _check_content_safety(
            worktree, dirty,
            max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
        )
        if unsafe is not None:
            unsafe.branch = current
            return unsafe
        _git(worktree, "add", "-A")
        result = _git(worktree, "commit", "-m", _COMMIT_SUBJECT)
        if result.returncode != 0:
            return PreserveResult(
                status="failed", reason="commit_failed", branch=current,
                detail=(result.stderr or result.stdout or "").strip()[:500] or None,
            )
        sha = _git_out(worktree, "rev-parse", "HEAD")

    remote = _resolve_push_remote(worktree, current)
    if remote is None:
        # Committed work is already safer than dirty work; a repo with no
        # usable remote simply cannot be pushed to, and that is recorded
        # rather than treated as a failure of the snapshot.
        return PreserveResult(
            status="preserved", commit_sha=sha, pushed=False,
            push_error="no_remote_configured", branch=current,
        )
    # No --force / --force-with-lease, ever: a rejected push means the remote
    # holds work this snapshot does not, and losing that is strictly worse
    # than leaving these commits local for a human.
    push = _git(worktree, "push", remote, f"HEAD:refs/heads/{current}")
    return PreserveResult(
        status="preserved",
        commit_sha=sha,
        pushed=push.returncode == 0,
        push_error=(
            None if push.returncode == 0
            else ((push.stderr or push.stdout or "").strip()[:500] or "push_failed")
        ),
        branch=current,
    )
