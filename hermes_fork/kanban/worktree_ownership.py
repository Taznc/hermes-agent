"""Kanban worktree ownership: a task may only tear down a worktree it created (tier T1, see FORK.md).

Incident 2026-09-27: archiving a card whose ``workspace_path`` pointed at the served ``next``
checkout ran ``git worktree remove`` on it. Every upstream guard passed (not the main checkout,
clean, fully pushed). Ownership is now proven by a git-admin marker (``hermes-kanban-owner.json``
in the worktree's private git dir, outside the working tree) that only dispatch-time creation
writes. Branch names and DB rows are caller-controlled and never authorize teardown.

Wiring (anchor ``kanban-worktree-ownership``):

* ``hermes_cli/kanban_db_workspace.py`` calls :func:`install` as its last statement, which
  replaces four of that module's worktree lifecycle functions with the ones below. Every
  in-module caller (``resolve_workspace``, ``_cleanup_workspace``,
  ``_try_cleanup_parent_workspaces``) and every external caller (``kanban_db_dispatch``,
  ``kanban_ops``) reaches them through the module global, so the rebinding covers all of them.
* ``hermes_cli/kanban_db.py::create_task`` calls :func:`reject_foreign_checkout`.

Upstream helpers are always looked up on the live module at call time, so monkeypatches of
``kanban_db_workspace._git`` etc. keep working. The replaced upstream bodies are pinned by hash in
``tests/hermes_fork/kanban/test_worktree_ownership.py``: an upstream edit to one of them fails that
test instead of being silently shadowed here.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

PROVENANCE_FILE = "hermes-kanban-owner.json"
_WS_MODULE = "hermes_cli.kanban_db_workspace"


def _ws():
    """The live ``hermes_cli.kanban_db_workspace`` module (possibly still initializing)."""
    return sys.modules.get(_WS_MODULE) or importlib.import_module(_WS_MODULE)


# ---------------------------------------------------------------------------------------- ownership


def task_owned_branch(task_id: str, branch_name: Optional[str]) -> str:
    """The one branch a task's worktree cleanup may act on: its recorded ``branch_name``, else the
    dispatcher's ``wt/<task-id>`` default."""
    return (branch_name or "").strip() or f"wt/{task_id}"


def checked_out_branch(path: Path) -> Optional[str]:
    """Branch checked out at ``path`` (``symbolic-ref --short HEAD``); ``None`` when detached or
    when git cannot answer. Callers must treat ``None`` as "not ours"."""
    return _ws()._kb._git_out(path, "symbolic-ref", "--short", "HEAD")


def provenance(path: Path) -> Optional[tuple[str, str]]:
    """``(task_id, branch)`` from the dispatch-time marker, or ``None``. Only a dispatcher-created
    worktree has this git-admin marker; DB rows and branch spelling alone are caller-controlled and
    cannot authorize teardown."""
    ws = _ws()
    git_dir = ws._git_abs_path(path, "--git-dir")
    if git_dir is None or ws._git_common_dir(path) == git_dir:
        return None
    try:
        owner = json.loads((git_dir / PROVENANCE_FILE).read_text(encoding="utf-8"))
        return owner["task_id"], owner["branch"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def worktree_owned_by_task(task_id: str, path: Path, branch_name: Optional[str]) -> bool:
    """Whether the checkout at ``path`` is on the branch the task owns AND carries its marker.

    A worktree task's ``workspace_path`` is a *request*, not proof of ownership: a caller can point
    it at a checkout that existed before the task did (the served ``next`` tree, a sibling task's
    worktree, a user's feature branch). Anything else is preserved with a WARNING naming both
    branches. A detached HEAD is never ours."""
    owned = task_owned_branch(task_id, branch_name)
    actual = checked_out_branch(path)
    if actual == owned and provenance(path) == (task_id, owned):
        return True
    _ws()._kb._log.warning(
        "Preserving worktree for task %s: %s is checked out on %s, "
        "but the task owns branch %s — checkout branch/provenance does not match this task",
        task_id, path, actual or "<detached HEAD>", owned,
    )
    return False


def reject_foreign_checkout(
    workspace_kind: Optional[str], workspace_path: Optional[str], branch_name: Optional[str]
) -> None:
    """Creation-time guard (``create_task``): a new worktree task cannot adopt ANY existing linked
    checkout.

    A caller may choose ``branch_name`` to match a foreign tree, so branch equality cannot establish
    ownership. Re-dispatch resumes the SAME task id, not a newly created task. A repo root is an
    anchor (dispatch materializes ``<repo>/.worktrees/<id>`` under it) and is left to the resolver;
    a path that does not exist yet is what the dispatcher creates."""
    if workspace_kind != "worktree" or not workspace_path:
        return
    ws = _ws()
    wp = Path(str(workspace_path)).expanduser()
    if not wp.is_dir() or not (wp / ".git").exists():
        return
    if not ws._is_linked_worktree_checkout(wp):
        return  # a main checkout: anchor semantics, never adopted verbatim
    actual = checked_out_branch(wp)
    owned = (branch_name or "").strip() or None
    repo_root = ws._repo_root_for_worktree_target(wp.parent)
    suggested = os.path.join(str(repo_root) if repo_root else "<repo>", ".worktrees", "<name>")
    raise ValueError(
        f"workspace_path {str(workspace_path)!r} is an existing git checkout on branch "
        f"{actual or '<detached HEAD>'}, which a new task cannot own "
        f"(task branch: {owned or 'wt/<task-id>'}). A worktree task must not adopt "
        "someone else's checkout — completing or archiving it would try to remove that "
        f"tree. Pass a NEW path such as {suggested!r} (the dispatcher creates the worktree)."
    )


def legacy_canonical_resume(task, path: Path, branch_name: str, conn: Optional[sqlite3.Connection]) -> bool:
    """Resume a pre-marker canonical checkout without claiming teardown ownership.

    The row/path alone is insufficient: require the default task branch, a canonical linked
    checkout and a *previous* worker-spawn record. No marker is written, so cleanup continues to
    preserve this tree for manual review."""
    if conn is None or branch_name != f"wt/{task.id}":
        return False
    ws = _ws()
    common = ws._git_common_dir(path)
    repo = common.parent if common is not None and common.name == ".git" else None
    if repo is None or ws._path_key(path) != ws._path_key(repo / ".worktrees" / task.id):
        return False
    if provenance(path) is not None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND worker_pid IS NOT NULL "
            "AND id != COALESCE(?, -1) LIMIT 1",
            (task.id, task.current_run_id),
        ).fetchone()
    except (sqlite3.Error, AttributeError):
        return False
    return row is not None


# ------------------------------------------------------- replacements for kanban_db_workspace.<name>


def ensure_git_worktree(repo_root: Path, target: Path, branch_name: str, task_id: Optional[str] = None) -> None:
    """Replaces ``_ensure_git_worktree``: materialize ``target`` as a linked git worktree under
    ``repo_root``. An existing checkout is reused only when it carries ``task_id``'s marker on the
    task's branch; otherwise it is refused. A freshly created tree gets the marker."""
    ws = _ws()
    target = target.expanduser()
    repo_common = ws._git_common_dir(repo_root)
    if target.exists() and repo_common is not None and ws._path_key(ws._git_common_dir(target)) == ws._path_key(repo_common):
        if task_id and worktree_owned_by_task(task_id, target, branch_name):
            return
        raise ValueError(f"Refusing to adopt existing unowned worktree at {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if ws._git_branch_exists(repo_root, branch_name):
        args = ["worktree", "add", str(target), branch_name]
    else:
        args = ["worktree", "add", "-b", branch_name, str(target), "HEAD"]
    result = ws._git(repo_root, *args, timeout=60)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )
    if task_id:
        git_dir = ws._git_abs_path(target, "--git-dir")
        if git_dir is None or ws._git_common_dir(target) == git_dir:
            raise RuntimeError(f"Cannot record worktree ownership for {target}")
        (git_dir / PROVENANCE_FILE).write_text(
            json.dumps({"task_id": task_id, "branch": branch_name}), encoding="utf-8"
        )


def anchored_worktree(repo_root: Path, task_id: str, branch_name: str) -> tuple[Path, str]:
    """Replaces ``_anchored_worktree``: the canonical ``<repo>/.worktrees/<task-id>``, marked."""
    target = repo_root / ".worktrees" / task_id
    _ws()._ensure_git_worktree(repo_root, target, branch_name, task_id)
    return target, branch_name


def resolve_worktree_workspace(task, *, board: Optional[str] = None,
                               conn: Optional[sqlite3.Connection] = None) -> tuple[Path, str]:
    """Replaces ``_resolve_worktree_workspace``. Same resolution as upstream (board
    ``default_workdir`` anchor, repo-root anchor, concrete path), except that an existing checkout
    is reused only with this task's marker; an unmarked checkout of the task's branch is resumed
    only via :func:`legacy_canonical_resume` (needs ``conn``) and otherwise refused; and a sibling's
    occupied path falls back to ``<repo>/.worktrees/<id>`` or is refused, never reused."""
    ws = _ws()
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        board_slug = board if board else ws._kb.get_current_board()
        board_default = (ws._kb.read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = ws._git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        return ws._anchored_worktree(repo_root, task.id, branch_name)

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and ws._is_linked_worktree_checkout(requested):
        actual_branch = ws._git_current_branch(requested)
        if actual_branch == branch_name and worktree_owned_by_task(task.id, requested, branch_name):
            return requested_resolved, actual_branch
        if actual_branch == branch_name and provenance(requested) is None:
            if legacy_canonical_resume(task, requested, branch_name, conn):
                ws._kb._log.warning(
                    "Resuming legacy unmarked worktree %s for task %s; automatic teardown disabled",
                    requested, task.id,
                )
                return requested_resolved, branch_name
            # Do not hide an unmarked checkout of this task's branch behind a
            # fresh fallback: that would strand its work (or adopt a foreign tree).
            raise ValueError(f"Refusing to dispatch task {task.id} into unowned worktree {requested}; "
                             "legacy recovery requires a canonical checkout and a prior worker run")
        # The requested path is an existing checkout of a DIFFERENT task's
        # branch (decompose children inherit the root's workspace_path
        # verbatim, so siblings all point here). Reusing it would run this task
        # on the other task's branch — silent cross-task provenance corruption,
        # unsafe under concurrency — so fall back to our own worktree.
        common = ws._git_common_dir(requested)
        fallback_root = common.parent if common and common.name == ".git" else None
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if ws._path_key(fallback.resolve(strict=False)) != ws._path_key(requested_resolved):
                ws._ensure_git_worktree(fallback_root, fallback, branch_name, task.id)
                return fallback.resolve(strict=False), branch_name
        raise ValueError(f"Refusing to dispatch task {task.id} into unowned worktree {requested}")

    repo_root = ws._git_toplevel(requested)
    if repo_root is not None and ws._path_key(requested_resolved) == ws._path_key(repo_root):
        return ws._anchored_worktree(repo_root, task.id, branch_name)

    repo_root = ws._repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    ws._ensure_git_worktree(repo_root, requested, branch_name, task.id)
    return requested, branch_name


def cleanup_worktree_workspace(task_id: str, path: str, branch_name: Optional[str] = None) -> None:
    """Replaces ``_cleanup_worktree_workspace``: remove a finished task's linked git worktree when
    it holds no work AND the task owns it.

    Upstream's gates stay (clean tree, every commit reachable from a remote-tracking ref, never the
    main checkout, no ``--force``). Added: the tree must be checked out on the branch the task owns
    (``branch_name`` or ``wt/<task-id>``) and carry this task's dispatch-time marker, checked again
    right before ``git worktree remove``; and only the auto-generated ``wt/<task-id>`` branch is
    deleted (upstream deleted any ``wt/*``). Best-effort: never blocks completion."""
    ws = _ws()
    try:
        from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
    except Exception:
        return  # CLI safety predicates unavailable — preserve
    try:
        wp = Path(path).expanduser()
        if not wp.is_dir():
            return
        common = ws._git_common_dir(wp)
        if common is None or common.name != ".git":
            return  # not a linked worktree of a normal repo — never guess
        repo_root = common.parent
        if ws._path_key(wp.resolve(strict=False)) == ws._path_key(repo_root.resolve(strict=False)):
            return  # never remove the main checkout
        if not worktree_owned_by_task(task_id, wp, branch_name):
            return  # someone else's checkout (served tree, sibling task, user branch)
        if _worktree_is_dirty(str(wp), str(repo_root)) or _worktree_has_unpushed_commits(str(wp)):
            ws._kb._log.info(
                "Preserving worktree for task %s: dirty or unpushed work at %s",
                task_id, wp,
            )
            return
        # Windows cannot delete a directory while this process has its current
        # directory inside it. Completed workers normally run from their own
        # linked worktree, so move this process back to the main checkout
        # before asking Git to remove the worktree.
        worktree_path = wp.resolve(strict=False)
        try:
            cwd = Path.cwd().resolve(strict=False)
        except OSError:
            # cwd was already deleted (a scratch-kind child's own workspace is
            # rmtree'd before this deferred parent cleanup runs, #33774). A
            # dead cwd cannot hold the worktree open, so leaving it is safe.
            cwd = None
        if cwd is None or cwd == worktree_path or cwd.is_relative_to(worktree_path):
            try:
                os.chdir(repo_root)
            except OSError as exc:
                ws._kb._log.warning(
                    "Preserving worktree for task %s: cannot leave %s for %s: %s",
                    task_id, cwd or "<deleted cwd>", repo_root, exc,
                )
                return
        # No --force: git's own dirty guard re-verifies at removal time, so if
        # the tree became dirty since our check (TOCTOU) removal fails safe.
        ws.release_lsp_clients(str(worktree_path))
        if not worktree_owned_by_task(task_id, wp, branch_name):
            return  # branch/marker changed after the earlier ownership check
        result = ws._git(repo_root, "worktree", "remove", str(wp), timeout=60)
        if result.returncode != 0:
            # Windows can retain a directory handle briefly after cwd changes.
            # Retry once without --force; Git still enforces its dirty guard.
            time.sleep(0.1)
            result = ws._git(repo_root, "worktree", "remove", str(wp), timeout=60)
        if result.returncode != 0:
            ws._kb._log.warning(
                "git worktree remove failed for task %s at %s: %s",
                task_id, wp, (result.stderr or result.stdout or "").strip(),
            )
            return
        ws._kb._log.debug("Removed worktree workspace: %s", wp)
        branch = task_owned_branch(task_id, branch_name)
        if branch == f"wt/{task_id}":
            ws._git(repo_root, "branch", "-D", branch, timeout=30)
    except Exception:
        pass  # best-effort — never block completion


# ------------------------------------------------------------------------------------------- anchor

#: upstream name -> fork replacement.
REPLACEMENTS = {
    "_ensure_git_worktree": ensure_git_worktree,
    "_anchored_worktree": anchored_worktree,
    "_resolve_worktree_workspace": resolve_worktree_workspace,
    "_cleanup_worktree_workspace": cleanup_worktree_workspace,
}


def install(module_name: str) -> None:
    """Rebind ``module_name``'s (``hermes_cli.kanban_db_workspace``) worktree lifecycle functions.

    Idempotent: a second call on an already-installed module is a no-op."""
    ws = sys.modules[module_name]
    for name, replacement in REPLACEMENTS.items():
        setattr(ws, name, replacement)
