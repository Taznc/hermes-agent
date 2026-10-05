"""Startup worktree pruner ownership: only reap trees Hermes itself created (tier T1, see FORK.md).

The startup pruner (``hermes_cli.worktree_ops._prune_stale_worktrees``, run by ``hermes``/TUI
launch and the cron tick) reaps clean, fully-pushed trees under ``<repo>/.worktrees/``. A served
or hand-managed checkout looks exactly like that (the 2026-09-27 incident removed a served ``next``
tree with every guard passing). This module adds ownership on top of upstream's gates:

* Creation marks: ``hermes -w`` (``_setup_worktree``) and subagent worktrees write
  ``hermes-prune-owner.json`` (``{path, branch}``) into the worktree's private git admin dir.
* A tree is reapable only when it is on a Hermes-minted branch prefix
  (:data:`HERMES_OWNED_BRANCH_PREFIXES`) AND its marker matches its path and branch. Anything else
  (trunks, user branches, detached HEADs, unmarked trees) gets verdict ``foreign-branch``.
* Ownership, cleanliness, push state and lock state are re-checked right before removal, and the
  removal never uses ``--force``.
* A lock whose reason is not exactly ``hermes pid=<pid>`` is treated as live (never unlocked).
* Orphan-branch deletion is disabled: a branch with no checkout has no marker to prove its creator.

Wiring (anchor ``worktree-prune-ownership``): ``hermes_cli/worktree_ops.py`` calls :func:`install`
as its last statement; ``tools/subagent_worktree.py`` calls :func:`mark_created_worktree`. The
replaced upstream bodies are pinned by hash in ``tests/hermes_fork/test_worktree_prune_ownership.py``.
"""

from __future__ import annotations

import concurrent.futures
import functools
import importlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

PRUNE_OWNER_FILE = "hermes-prune-owner.json"

# Branch prefixes Hermes itself mints for scratch worktrees under ``.worktrees/``:
# ``hermes/`` (``hermes -w``), ``hermes-subagent/`` (subagent isolation), ``wt/`` (kanban
# default) and ``pr-`` (review checkouts). The pruner requires BOTH this prefix and a creation
# marker. A tree on anything else — a trunk such as ``main``/``next``, a user's feature branch, a
# detached HEAD — was not created by Hermes for scratch use: preserved, never guessed.
HERMES_OWNED_BRANCH_PREFIXES = ("hermes/", "hermes-subagent/", "wt/", "pr-")

FOREIGN_VERDICT = "foreign-branch"
FOREIGN_REASON = "no matching Hermes creation marker/branch"

_OPS_MODULE = "hermes_cli.worktree_ops"
logger = logging.getLogger("cli")  # the same logger hermes_cli.worktree_ops uses


def _ops():
    """The live ``hermes_cli.worktree_ops`` module (possibly still initializing)."""
    return sys.modules.get(_OPS_MODULE) or importlib.import_module(_OPS_MODULE)


# ------------------------------------------------------------------------------------ creation mark


def _prune_owner_path(worktree_path: str) -> Optional[Path]:
    """Git-admin location, outside the working tree (not affected by git status)."""
    ops = _ops()
    try:
        git_dir = ops._git_out(["rev-parse", "--path-format=absolute", "--git-dir"], worktree_path)
        common = ops._git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], worktree_path)
        if git_dir and common and Path(git_dir).resolve() != Path(common).resolve():
            return Path(git_dir) / PRUNE_OWNER_FILE
    except Exception:
        pass
    return None


def mark_prune_owned_worktree(worktree_path: str, branch: str) -> None:
    """Record that Hermes created ``worktree_path`` on ``branch``; raises when it cannot."""
    marker = _prune_owner_path(worktree_path)
    if marker is None:
        raise RuntimeError(f"Cannot mark worktree ownership: {worktree_path}")
    marker.write_text(json.dumps({"path": str(Path(worktree_path).resolve()), "branch": branch}), encoding="utf-8")


def is_prune_owned_worktree(worktree_path: str, branch: str) -> bool:
    marker = _prune_owner_path(worktree_path)
    if marker is None:
        return False
    try:
        owner = json.loads(marker.read_text(encoding="utf-8"))
        return owner == {"path": str(Path(worktree_path).resolve()), "branch": branch}
    except (OSError, ValueError, TypeError):
        return False


def mark_created_worktree(worktree_path: str, branch: str, log: logging.Logger, message: str) -> None:
    """Fail-soft :func:`mark_prune_owned_worktree` for creation sites: an unmarked tree is merely
    never auto-pruned, so a marking failure is logged (``message % (path, error)``), not raised."""
    try:
        mark_prune_owned_worktree(worktree_path, branch)
    except Exception as exc:
        log.warning(message, worktree_path, exc)


# ---------------------------------------------------------------------------------------- ownership


def worktree_symbolic_branch(worktree_path: str, timeout: float = 5) -> Optional[str]:
    """Branch checked out at *worktree_path* via ``symbolic-ref --short HEAD``; None when detached
    or when git cannot answer. Fails SAFE toward None (callers treat None as not-ours)."""
    try:
        return _ops()._git_out(["symbolic-ref", "--short", "HEAD"], worktree_path, timeout=timeout) or None
    except Exception:
        return None


def worktree_on_hermes_owned_branch(worktree_path: str, timeout: float = 5) -> tuple[bool, Optional[str]]:
    """``(owned, branch)``: whether the tree's checked-out branch carries a Hermes-minted prefix."""
    branch = worktree_symbolic_branch(worktree_path, timeout=timeout)
    return bool(branch and branch.startswith(HERMES_OWNED_BRANCH_PREFIXES)), branch


def owned_for_prune(worktree_path: str, timeout: float = 5) -> tuple[bool, Optional[str]]:
    """``(reapable_by_ownership, branch)``: Hermes branch prefix AND a matching creation marker."""
    owned, branch = worktree_on_hermes_owned_branch(worktree_path, timeout=timeout)
    return owned and is_prune_owned_worktree(worktree_path, branch or ""), branch


# ---------------------------------------------------------- replacements for worktree_ops.<name>


def worktree_lock_is_live(repo_root: str, worktree_path: str, timeout: int = 10):
    """Replaces ``_worktree_lock_is_live``. Lock state: ``"live"`` (running PID, foreign or unknown
    lock), ``"dead"`` (exact Hermes PID reason and PID gone), None (unlocked).

    ``hermes -w`` locks with reason ``hermes pid=<pid>``; only those stale locks may be unlocked
    (upstream treated any other reason as a dead leftover). Fails SAFE toward "live".
    """
    ops = _ops()
    try:
        listing = ops._git_out(["worktree", "list", "--porcelain"], repo_root, timeout=timeout)
    except Exception:
        listing = None
    if listing is None:
        return "live"

    target = Path(worktree_path).resolve()
    current: Optional[Path] = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            try:
                current = Path(line[len("worktree "):].strip()).resolve()
            except Exception:
                current = None
        elif line == "locked" or line.startswith("locked "):
            if current != target:
                continue
            reason = line[len("locked"):].strip()
            m = re.fullmatch(r"hermes pid=([0-9]+)", reason)
            if not m:
                # A foreign lock may protect a live deployment even on a tree
                # originally created by Hermes. Never unlock it.
                return "live"
            pid = int(m.group(1))
            if pid == os.getpid():
                return "live"
            try:
                from gateway.status import _pid_exists
                return "live" if _pid_exists(pid) else "dead"
            except Exception:
                return "live"
    return None


def reap_prune_verdicts(repo_root: str, verdicts: list, stale_work_cutoff: float) -> tuple[list, set]:
    """Replaces ``_reap_prune_verdicts``. Phase 3, serial unlock / remove / branch -D ->
    ``(preserved_stale, kept_branches)``.

    Classification is a snapshot, so ownership, cleanliness, push state and the lock are re-checked
    immediately before mutation; removal never uses ``--force`` (git's own dirty/lock guards stay
    in force). Branch deletion is gated on ``worktree remove`` succeeding.
    """
    ops = _ops()
    reasons = {**ops._PRESERVE_REASONS, FOREIGN_VERDICT: FOREIGN_REASON}
    preserved_stale: list = []
    kept_branches: set = set()
    for entry, mtime, force, verdict, lock_state in verdicts:
        reason = reasons.get(verdict)
        if reason:
            if mtime <= stale_work_cutoff:
                preserved_stale.append(f"{entry.name} ({reason})")
            continue
        if verdict == "locked-live":
            logger.debug("Skipping live-locked worktree: %s", entry.name)
            continue

        # Classification is a snapshot. Branch/provenance can change before the
        # serial removal; a forced remove must recheck both immediately beforehand.
        owned, current = worktree_on_hermes_owned_branch(str(entry), timeout=5)
        if not owned or not is_prune_owned_worktree(str(entry), current or ""):
            logger.warning("Preserving worktree %s: ownership changed before removal", entry)
            continue
        if ops._worktree_is_dirty(str(entry), repo_root, timeout=5):
            logger.debug("Preserving worktree %s: state changed or unpushed at removal", entry)
            continue
        if ops._worktree_has_unpushed_commits(str(entry), timeout=5) and not (
            ops._worktree_commits_all_merged_upstream(str(entry), timeout=30)
            or ops._worktree_branch_pr_merged(str(entry), timeout=15)
            or (verdict == "reap-keep-branch" and ops._worktree_branch_pushed_exact(
                str(entry), ops._fetch_remote_branch_heads(repo_root, timeout=10)))
        ):
            continue
        # A lock may have been installed or replaced after classification.
        # Only unlock a currently stale Hermes PID lock; Git itself refuses
        # removal if another actor locks the tree before the remove call.
        current_lock = ops._worktree_lock_is_live(repo_root, str(entry), timeout=5)
        if current_lock == "live" or current_lock != lock_state:
            continue
        if current_lock == "dead":
            if ops._git(["worktree", "unlock", str(entry)], repo_root, timeout=10).returncode != 0:
                logger.warning("Failed to unlock dead worktree %s", entry.name)
                continue

        try:
            branch = worktree_symbolic_branch(str(entry), timeout=5)
            if branch != current or not is_prune_owned_worktree(str(entry), branch or ""):
                continue
            remove_result = ops._git(["worktree", "remove", str(entry)], repo_root, timeout=15)
            if remove_result.returncode != 0:
                logger.debug("Failed to remove worktree %s: %s", entry.name, remove_result.stderr.strip())
                continue
            if branch and verdict == "reap-keep-branch":
                kept_branches.add(branch)
            elif branch:
                # Unattended sweep: the repository's reference-transaction hook must not run.
                ops._git(["branch", "-D", branch], repo_root,
                         stdin=subprocess.DEVNULL, env=ops.noninteractive_git_env())
            logger.debug("Pruned stale worktree: %s (force=%s)", entry.name, force)
        except Exception as e:
            logger.debug("Failed to prune worktree %s: %s", entry.name, e)
    return preserved_stale, kept_branches


def prune_orphaned_branches(repo_root: str, protect: Optional[set] = None) -> None:
    """Replaces ``_prune_orphaned_branches`` with a no-op. An orphan branch has no checkout/admin
    marker left to establish its creator; prefix-only deletion could destroy a hand-managed branch.
    Orphan cleanup is left to an explicit, user-reviewed command."""
    logger.debug("Orphan-branch pruning disabled (no creation marker survives a removed checkout): %s", repo_root)


# --------------------------------------------------------------- wrappers around worktree_ops.<name>


def _wrap_setup_worktree(original):
    @functools.wraps(original)
    def _setup_worktree(*args, **kwargs):
        info = original(*args, **kwargs)
        if info:
            mark_created_worktree(
                info["path"], info["branch"], logger,
                "Worktree %s created but cannot mark ownership; startup pruner will preserve it: %s",
            )
        return info

    setattr(_setup_worktree, "__fork_wrapped__", original)
    return _setup_worktree


def _wrap_classify_prune_candidates(original):
    @functools.wraps(original)
    def _classify_prune_candidates(repo_root: str, candidates: list) -> list:
        """Ownership first: a candidate that is not :func:`owned_for_prune` gets verdict
        ``foreign-branch`` and never reaches upstream's dirty/unpushed/lock classification."""
        candidates = list(candidates)
        if not candidates:
            return original(repo_root, candidates)

        def _owned(item):
            return owned_for_prune(str(item[0]), timeout=5)

        # Same pool shape and serial fallback as upstream's classification.
        workers = max(1, min(8, (os.cpu_count() or 4), len(candidates)))
        try:
            if workers > 1:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="hermes-wt-owner"
                ) as pool:
                    ownership = list(pool.map(_owned, candidates))
            else:
                ownership = [_owned(c) for c in candidates]
        except Exception as e:
            logger.debug("Parallel worktree ownership check failed (%s); serial", e)
            ownership = [_owned(c) for c in candidates]
        verdicts: list = [None] * len(candidates)
        mine: list = []
        for i, ((entry, mtime, force), (owned, branch)) in enumerate(zip(candidates, ownership)):
            if owned:
                mine.append(i)
                continue
            logger.debug(
                "Preserving worktree %s: checked out on %s, not a Hermes-owned branch (%s)",
                entry, branch or "<detached HEAD>", ", ".join(HERMES_OWNED_BRANCH_PREFIXES),
            )
            verdicts[i] = (entry, mtime, force, FOREIGN_VERDICT, None)
        if mine:
            for i, verdict in zip(mine, original(repo_root, [candidates[i] for i in mine])):
                verdicts[i] = verdict
        return verdicts

    setattr(_classify_prune_candidates, "__fork_wrapped__", original)
    return _classify_prune_candidates


# ------------------------------------------------------------------------------------------- anchor

#: upstream name -> fork replacement.
REPLACEMENTS = {
    "_worktree_lock_is_live": worktree_lock_is_live,
    "_reap_prune_verdicts": reap_prune_verdicts,
    "_prune_orphaned_branches": prune_orphaned_branches,
}
#: upstream name -> wrapper factory (the wrapper keeps the original as ``__fork_wrapped__``).
WRAPPERS = {
    "_setup_worktree": _wrap_setup_worktree,
    "_classify_prune_candidates": _wrap_classify_prune_candidates,
}


def install(module_name: str) -> None:
    """Rebind ``module_name``'s (``hermes_cli.worktree_ops``) pruner/creation functions.

    Idempotent: already-replaced and already-wrapped names are left alone."""
    ops = sys.modules[module_name]
    for name, replacement in REPLACEMENTS.items():
        setattr(ops, name, replacement)
    for name, wrap in WRAPPERS.items():
        current = getattr(ops, name)
        if getattr(current, "__fork_wrapped__", None) is not None:
            continue
        setattr(ops, name, wrap(current))
