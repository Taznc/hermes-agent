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
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

_log = logging.getLogger(__name__)

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
    """``git -C cwd args``; never raises — a timeout is folded into a non-zero
    ``CompletedProcess`` (returncode 124, by shell convention) rather than
    propagating ``subprocess.TimeoutExpired``. A push that times out after the
    commit has already landed must still return a normal failed-push result
    with the commit SHA intact; letting the exception escape here is exactly
    what previously lost that SHA (and the whole board event) to the
    catch-all in :func:`preserve_task_work`.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(cwd), *args], returncode=124,
            stdout="", stderr=f"timed out after {exc.timeout}s",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(cwd), *args], returncode=127,
            stdout="", stderr=str(exc),
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

    A remote being CONFIGURED but having no cached tracking refs (never
    fetched, or a branch pushed with ``--no-track``) is ambiguous, not proof
    that nothing is unpushed — only the true absence of any remote at all
    settles that. Treating an empty ``refs/remotes`` as "nothing to push"
    whenever a remote exists silently drops real commits (see the reviewer
    finding on this card): a configured-but-uncached remote must still be
    tried.
    """
    remotes = _git(worktree, "remote")
    if remotes.returncode != 0:
        return True
    if not (remotes.stdout or "").strip():
        return False  # no remote at all: nothing to be unpushed against
    remote_refs = _git(worktree, "for-each-ref", "--format=%(refname)", "refs/remotes")
    if remote_refs.returncode != 0:
        return True
    if not (remote_refs.stdout or "").strip():
        return True  # a remote IS configured but we have no cached baseline
    unpushed = _git(worktree, "log", "--oneline", "HEAD", "--not", "--remotes")
    if unpushed.returncode != 0:
        return True
    return bool((unpushed.stdout or "").strip())


def _dirty_paths(worktree: Path) -> Optional[list[str]]:
    """Paths git reports as changed (gitignored files are excluded by git
    itself), or ``None`` when the status probe failed.

    ``--porcelain -z`` is a fixed format: two status characters, a space, then
    the path verbatim to a NUL. Sliced by position rather than split, so a path
    that itself begins with a space is read correctly. A rename/copy entry is
    followed by a second record holding its ORIGINAL path, which must be
    consumed as data and never treated as another candidate.
    """
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
        if len(entry) < 4:
            continue
        if entry[0] in {"R", "C"} or entry[1] in {"R", "C"}:
            skip_next = True
        path = entry[3:]
        if path:
            paths.append(path)
    return paths


def _expand_candidates(worktree: Path, paths: list[str]) -> list[str]:
    """Expand git's directory-collapsed untracked entries (``dir/``) into the
    real files underneath, so a guard can never be evaded by collapsing.

    Files inside an expanded directory can themselves be gitignored (a
    ``.gitignore`` nested inside the collapsed directory, or a pattern that
    only matches once the directory is walked) — ``git status`` never lists
    them as top-level candidates, but expansion can surface them as children.
    Those are filtered out via ``git check-ignore`` so the AC's "gitignored
    files remain excluded" holds for descendants too, not just top-level
    entries.
    """
    expanded: list[str] = []
    to_check: list[str] = []
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
                to_check.append(child.relative_to(worktree).as_posix())
    if to_check:
        ignored_result = subprocess.run(
            ["git", "-C", str(worktree), "check-ignore", "--stdin", "-z"],
            input="\0".join(to_check) + "\0",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT, check=False,
        )
        ignored_set = {
            p for p in (ignored_result.stdout or "").split("\0") if p
        }
        expanded.extend(p for p in to_check if p not in ignored_set)
    else:
        expanded.extend(to_check)
    return expanded


def _generated_component(path: str) -> Optional[str]:
    """The first generated/vendored path component of *path*, if any."""
    for part in Path(path).parts:
        if part in _GENERATED_DIR_NAMES:
            return part
    return None


def _looks_like_secret_filename(path: str) -> bool:
    """True when the filename itself marks the file as credential-bearing.

    The name is compared with surrounding whitespace and trailing dots removed:
    git reports paths verbatim, so ``" id_rsa"`` and ``"id_rsa "`` are real,
    creatable filenames that a naive exact match would wave through while the
    file is still an SSH private key. Matching the normalized name closes that
    evasion without altering the path used to read the file.
    """
    lowered = Path(path).name.strip().rstrip(".").lower()
    if lowered in _SECRET_FILE_NAMES:
        return True
    if lowered.endswith(_SECRET_FILE_SUFFIXES):
        return True
    return any(
        lowered.startswith(prefix) and lowered != prefix.rstrip(".")
        for prefix in _SECRET_FILE_PREFIXES
    )


def _contains_credential(file_path: Path) -> Optional[bool]:
    """Whether the redactor finds a credential in *file_path*'s text.

    ``None`` means the scan could not run at all (redactor import failed) —
    distinct from ``False`` (scanned, clean) so the caller can fail CLOSED on
    an unavailable safety dependency instead of silently treating "could not
    scan" as "safe". Reuses ``agent.redact`` rather than a second pattern
    list so the preservation guard and every other Hermes safety boundary
    agree on what a credential looks like. ``code_file=True`` keeps ordinary
    source constants (``MAX_TOKENS=...``, test fixtures) from tripping it;
    prefix-matched real credentials, private-key blocks and JWTs still do.
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
        return None
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
        if full.is_file() and not full.is_symlink():
            scan = _contains_credential(full)
            if scan is None:
                return PreserveResult(
                    status="unsafe", reason="credential_scan_unavailable",
                    detail=f"{rel} could not be scanned for credentials "
                           f"(the safety dependency is unavailable)",
                )
            if scan:
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


def _stale_pid_lock(lock_path: Path) -> bool:
    """True when *lock_path* names a pid that is no longer alive (or the file
    is unreadable/corrupt, which is treated the same as abandoned)."""
    try:
        content = lock_path.read_text(encoding="ascii").strip()
        pid = int(content)
    except (OSError, ValueError):
        return True
    return not _pid_alive(pid)


def _acquire_pid_lock(lock_path: Path) -> bool:
    """Exclusive lock via atomic file creation (``O_CREAT | O_EXCL``).

    Portable across POSIX and Windows with no ``fcntl``/``msvcrt`` split —
    both platforms make ``open(O_EXCL)`` a single atomic syscall, so there is
    no platform branch and no degraded "always acquired" fallback. Self-
    healing: a lock left behind by a process that has since died is stolen
    rather than left to block preservation forever, since this lock guards a
    best-effort snapshot, not repository correctness. The steal path has a
    narrow TOCTOU (two callers could both observe staleness at once); the
    worst outcome is two safety commits, never lost work, which matches the
    existing tolerance for the concurrency guarantee on a crash-recovery edge
    case.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not _stale_pid_lock(lock_path):
            return False
        with contextlib.suppress(OSError):
            lock_path.unlink()
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


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
    owns this work, and waiting for it would only duplicate the outcome. When
    exclusion cannot even be established (the git dir is unresolvable), that
    fails CLOSED — proceeding unlocked is exactly the race this guards
    against, not a safe default.
    """
    git_dir = _git_out(worktree, "rev-parse", "--path-format=absolute", "--git-dir")
    if not git_dir:
        yield False
        return
    name = f"hermes-kanban-preserve-{task_id or 'worktree'}.lock"
    lock_path = Path(git_dir) / name
    if not _acquire_pid_lock(lock_path):
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


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


# ---------------------------------------------------------------------------
# Task-level entry point: ownership gating + board record
# ---------------------------------------------------------------------------


def _preservation_config() -> dict:
    """``kanban.worker_preservation`` from config, or ``{}`` when unreadable.

    Fails OPEN (preservation enabled) on a config error: losing a worker's
    work because config.yaml was momentarily unparseable is the worse outcome.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = (cfg.get("kanban", {}) or {}) if isinstance(cfg, dict) else {}
        value = section.get("worker_preservation")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    """Late-bound so the dispatcher owns the one liveness implementation."""
    from hermes_cli.kanban_db_dispatch import _pid_alive as impl

    return bool(impl(pid))


def _ownership_skip(
    row, expected_run_id: Optional[int], *, worker_pid: Optional[int] = None,
) -> Optional[PreserveResult]:
    """Fail-closed ownership gate: ``None`` to proceed, else the skip verdict.

    Two independent ways this call can be the wrong one to snapshot:

    * **Stale run.** A reclaim path carrying run N must not commit work the
      task's CURRENT run N+1 is still producing — that would attribute a live
      worker's half-finished edits to a dead attempt.
    * **Live worker.** While the owning PID is alive, only that process may
      snapshot; anyone else races the worker's own editor and can capture a
      file mid-write.

    ``worker_pid`` lets a caller supply the pid it captured BEFORE its own
    lifecycle UPDATE cleared ``tasks.worker_pid`` to NULL in the same
    transaction (``complete_task``/``block_task``/``request_review``/
    ``archive_task`` all do this) — reading the row's live column here would
    otherwise see NULL and wrongly treat an archived-while-running task as
    ownerless. ``None`` (the default) falls back to the row's own column,
    which is correct for callers that preserve BEFORE clearing it (the
    reclaim paths).
    """
    if expected_run_id is not None:
        current = row["current_run_id"]
        if current is None or int(current) != int(expected_run_id):
            return PreserveResult(
                status="skipped", reason="stale_run",
                detail=f"expected run {expected_run_id}, task is on {current}",
            )
    pid = worker_pid if worker_pid is not None else row["worker_pid"]
    if pid and int(pid) != os.getpid() and _pid_alive(int(pid)):
        return PreserveResult(
            status="skipped", reason="worker_alive",
            detail=f"worker pid {int(pid)} still running",
        )
    return None


def _record(conn, task_id: str, result: PreserveResult, run_id: Optional[int]) -> None:
    """Append the board-visible record of one preservation attempt.

    Only outcomes a human may need to act on are recorded: a snapshot that
    happened (with its SHA and push result) and one that refused unsafe
    content or failed. Skips and no-ops are the common case and would be pure
    event-log noise.
    """
    from hermes_cli import kanban_db as _kb

    if result.status == "preserved":
        kind, payload = "work_preserved", {
            "commit_sha": result.commit_sha,
            "pushed": result.pushed,
            "push_error": result.push_error,
            "branch": result.branch,
        }
    elif result.status in {"unsafe", "failed"}:
        kind, payload = "work_preservation_failed", {
            "status": result.status,
            "reason": result.reason,
            "detail": result.detail,
            "branch": result.branch,
        }
    else:
        return
    try:
        with _kb.write_txn(conn):
            _kb._append_event(conn, task_id, kind, payload, run_id=run_id)
    except Exception:
        _log.warning("kanban: could not record %s for task %s", kind, task_id)


def preserve_task_work(
    conn, task_id: str, *, expected_run_id: Optional[int] = None,
    known_worker_pid: Optional[int] = None,
) -> PreserveResult:
    """Preserve one task's own worktree, gated on ownership, and record it.

    Safe to call from any lifecycle path and safe to call twice: a second call
    finds nothing to preserve, and a genuinely concurrent one stands down on
    the worktree lock. Never raises — a preservation failure must not block the
    completion/reclaim it is attached to.

    ``known_worker_pid``: pass the pid the caller captured BEFORE its own
    lifecycle UPDATE cleared ``tasks.worker_pid`` in the same transaction
    (``complete_task``, ``block_task``, ``request_review``, ``archive_task``
    all clear it as part of the terminal-status write). Without this, the
    ownership check below reads a column that already says NULL and treats
    a still-running worker as absent — letting preservation race and commit
    over its half-written tree. Omit it (the reclaim paths do) when the
    caller preserves BEFORE any column clear, so the row's own value is
    still live and correct.

    A genuinely unexpected exception (not the git-timeout/OSError cases
    ``_git`` already folds into an ordinary failed push) still needs its own
    ``work_preservation_failed`` event: silently returning a bare ``failed``
    result here means a human auditing the board sees nothing happened, when
    in fact preservation ran and lost. Best-effort HEAD lookup so a commit
    that landed before the exception is still named in the record.
    """
    try:
        return _preserve_task_work(
            conn, task_id, expected_run_id=expected_run_id,
            known_worker_pid=known_worker_pid,
        )
    except Exception as exc:  # never block a lifecycle transition
        _log.warning("kanban: preservation errored for task %s: %s", task_id, exc)
        result = PreserveResult(
            status="failed", reason="preservation_error",
            detail=str(exc)[:500] or None,
            commit_sha=_best_effort_head_sha(conn, task_id),
        )
        with contextlib.suppress(Exception):
            _record(conn, task_id, result, None)
        return result


def _best_effort_head_sha(conn, task_id: str) -> Optional[str]:
    """The worktree's current HEAD, or ``None`` on any failure — used only to
    enrich an already-failed record, never to make a decision."""
    try:
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row or row["workspace_kind"] != "worktree" or not row["workspace_path"]:
            return None
        worktree = Path(row["workspace_path"]).expanduser()
        if not worktree.is_dir():
            return None
        return _git_out(worktree, "rev-parse", "HEAD")
    except Exception:
        return None


def _preserve_task_work(
    conn, task_id: str, *, expected_run_id: Optional[int],
    known_worker_pid: Optional[int] = None,
) -> PreserveResult:
    cfg = _preservation_config()
    if cfg.get("enabled") is False:
        return PreserveResult(status="skipped", reason="disabled")
    row = conn.execute(
        "SELECT workspace_kind, workspace_path, branch_name, worker_pid, current_run_id "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return PreserveResult(status="skipped", reason="task_not_found")
    if row["workspace_kind"] != "worktree" or not row["workspace_path"]:
        return PreserveResult(status="skipped", reason="not_a_worktree_workspace")
    worktree = Path(row["workspace_path"]).expanduser()
    if not worktree.is_dir():
        return PreserveResult(status="skipped", reason="workspace_missing")
    # Ownership is a claim in the DB, not a fact — a corrupt or aliased row
    # (two tasks pointing at the same workspace_path) must never let this
    # call commit and attribute another task's live worktree to task_id. A
    # legitimate worktree path is unique to its owning task
    # (``<repo>/.worktrees/<task-id>``), so any OTHER row claiming the exact
    # same path is definitionally ambiguous and this call stands down.
    conflict = conn.execute(
        "SELECT 1 FROM tasks WHERE workspace_kind = 'worktree' "
        "AND workspace_path = ? AND id != ? LIMIT 1",
        (row["workspace_path"], task_id),
    ).fetchone()
    if conflict is not None:
        return PreserveResult(status="skipped", reason="workspace_path_conflict")
    skip = _ownership_skip(row, expected_run_id, worker_pid=known_worker_pid)
    if skip is not None:
        return skip

    branch = (row["branch_name"] or "").strip() or f"wt/{task_id}"
    result = preserve_worktree(
        worktree, branch, task_id=task_id,
        max_file_bytes=_positive_int(cfg.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES),
        max_total_bytes=_positive_int(cfg.get("max_total_bytes"), DEFAULT_MAX_TOTAL_BYTES),
    )
    _record(conn, task_id, result, row["current_run_id"])
    return result


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
