"""Tests for worktree workspace teardown at task completion/archive.

Covers the ownership gap where kanban ``worktree`` workspaces were never
reaped by anything: ``_cleanup_workspace`` preserved them by design, the CLI
startup pruner explicitly skips ``t_*`` worktrees ("dispatcher-driven
lifecycle"), and ``kanban gc`` only swept scratch. A completed or archived
task's linked worktree is now removed when — and only when — it provably
holds no work: clean working tree and every commit reachable from a
remote-tracking ref. Any doubt preserves the worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kbw._ensure_git_worktree(repo, target, branch or f"wt/{task_id}", task_id)
    return target


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


# ---------------------------------------------------------------------------
# _cleanup_worktree_workspace unit behavior
# ---------------------------------------------------------------------------


def test_clean_pushed_worktree_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_aaaa1111")
    kbw._cleanup_worktree_workspace("t_aaaa1111", str(wt))
    assert not wt.exists()
    # auto-generated task branch goes with it
    assert not _branch_exists(repo, "wt/t_aaaa1111")
    # main checkout untouched
    assert (repo / "README.md").exists()


def test_cleanup_leaves_a_worktree_cwd_before_removal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot remove a worktree that is the process's current directory."""
    wt = _make_worktree(repo, "t_cwd112425")
    real_git = kbw._git

    def windows_git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
        if args[:2] == ("worktree", "remove") and Path.cwd().is_relative_to(wt):
            return subprocess.CompletedProcess(
                ["git", *args], 1, stderr="Permission denied: current directory"
            )
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", windows_git)
    monkeypatch.chdir(wt)
    kbw._cleanup_worktree_workspace("t_cwd112425", str(wt))

    assert Path.cwd() == repo
    assert not wt.exists()


def test_cleanup_proceeds_when_cwd_was_deleted(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred parent cleanup (#33774) runs after the child's scratch cwd was
    rmtree'd; a dead cwd must not preserve a clean, pushed worktree."""
    wt = _make_worktree(repo, "t_deadcwd113073")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    scratch.rmdir()

    kbw._cleanup_worktree_workspace("t_deadcwd113073", str(wt))

    assert not wt.exists()
    assert not _branch_exists(repo, "wt/t_deadcwd113073")


def test_cleanup_retries_worktree_removal_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief Windows directory-handle delay gets one safe retry."""
    wt = _make_worktree(repo, "t_retry112425")
    real_git = kbw._git
    attempts = 0

    def delayed_remove(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
        nonlocal attempts
        if args[:2] == ("worktree", "remove"):
            attempts += 1
            if attempts == 1:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, stderr="Permission denied: handle pending"
                )
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", delayed_remove)
    monkeypatch.setattr(kbw.time, "sleep", lambda _delay: None)
    kbw._cleanup_worktree_workspace("t_retry112425", str(wt))

    assert attempts == 2
    assert not wt.exists()


def test_dirty_worktree_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_bbbb2222", str(wt))
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_unpushed_commits_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc3333")
    (wt / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(wt), "add", "work.txt")
    _git("-C", str(wt), "commit", "-m", "local work")
    kbw._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert wt.is_dir()


def test_custom_branch_survives_worktree_removal(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd4444", branch="feature/custom")
    kbw._cleanup_worktree_workspace("t_dddd4444", str(wt), "feature/custom")
    assert not wt.exists()
    # only auto-generated wt/* branches are deleted
    assert _branch_exists(repo, "feature/custom")


def test_main_checkout_never_removed(repo: Path) -> None:
    kbw._cleanup_worktree_workspace("t_eeee5555", str(repo))
    assert repo.is_dir()
    assert (repo / "README.md").exists()


def test_non_git_dir_preserved(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    kbw._cleanup_worktree_workspace("t_ffff6666", str(plain))
    assert plain.is_dir()


# ---------------------------------------------------------------------------
# Branch ownership: a task may only remove the worktree on ITS branch
# ---------------------------------------------------------------------------
#
# Incident: a task was created with workspace_kind='worktree' and
# workspace_path pointing at the SERVED checkout of branch ``next`` (itself a
# linked worktree of the main repo). Archiving it 13s later ran
# ``git worktree remove`` on that tree — every pre-existing guard passed (not
# the main checkout, clean, fully pushed) — and the live services lost their
# source. Nothing checked that the tree was on the branch the task owned.


def _served_worktree(repo: Path, name: str, branch: str) -> Path:
    """A linked worktree on a long-lived, fully-pushed branch (like a served ``next``)."""
    _git("-C", str(repo), "branch", branch, "HEAD")
    _git("-C", str(repo), "push", "origin", branch)
    target = repo.parent / name
    _git("-C", str(repo), "worktree", "add", str(target), branch)
    return target


def test_worktree_on_foreign_branch_preserved_when_task_has_no_branch(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """(a) The incident shape: branch_name None -> owned branch is wt/<id>, the
    tree is on ``next`` -> preserved, WARNING names both branches."""
    served = _served_worktree(repo, "served-next", "next")
    assert (served / "README.md").exists()
    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        kbw._cleanup_worktree_workspace("t_0b571cee", str(served), None)
    assert served.is_dir(), "a clean, pushed tree on another branch must survive archive"
    assert (served / "README.md").exists()
    assert _branch_exists(repo, "next")
    listed = _git("-C", str(repo), "worktree", "list", "--porcelain")
    assert f"worktree {served.resolve()}" in listed
    msg = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "next" in msg and "wt/t_0b571cee" in msg, msg


def test_worktree_on_foreign_branch_preserved_when_task_names_another_branch(repo: Path) -> None:
    """branch_name set but the tree is on something else -> preserved."""
    served = _served_worktree(repo, "served-main-alias", "release/1.0")
    kbw._cleanup_worktree_workspace("t_dddd0001", str(served), "wt/t_dddd0001")
    assert served.is_dir()
    assert _branch_exists(repo, "release/1.0")


def test_detached_head_worktree_preserved(repo: Path) -> None:
    """A detached HEAD has no branch; it can never be the task's -> preserved."""
    target = repo.parent / "detached"
    _git("-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD")
    kbw._cleanup_worktree_workspace("t_dddd0002", str(target), None)
    assert target.is_dir()


def test_worktree_on_own_wt_branch_still_removed(repo: Path) -> None:
    """(b) Existing behaviour: a clean, pushed tree on wt/<id> IS removed."""
    wt = _make_worktree(repo, "t_bbbb0001")
    assert _git("-C", str(wt), "symbolic-ref", "--short", "HEAD").strip() == "wt/t_bbbb0001"
    kbw._cleanup_worktree_workspace("t_bbbb0001", str(wt), None)
    assert not wt.exists()
    assert not _branch_exists(repo, "wt/t_bbbb0001")


def test_worktree_on_own_custom_branch_still_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb0002", branch="feature/mine")
    kbw._cleanup_worktree_workspace("t_bbbb0002", str(wt), "feature/mine")
    assert not wt.exists()
    assert _branch_exists(repo, "feature/mine")


def test_archive_task_preserves_served_checkout_on_other_branch(kanban_home: Path, repo: Path) -> None:
    """End-to-end replay of the incident through archive_task -> _cleanup_workspace.

    The DB row is written directly (as the incident row was) to bypass the
    creation-time guard and prove the cleanup guard alone is sufficient."""
    served = _served_worktree(repo, "served-next-e2e", "next")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="chat-created", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=NULL "
                "WHERE id=?",
                (str(served), tid),
            )
        assert kb.archive_task(conn, tid)
    assert served.is_dir()
    assert (served / "README.md").exists()
    assert _branch_exists(repo, "next")

def test_legacy_dispatch_does_not_adopt_foreign_checkout(kanban_home: Path, repo: Path) -> None:
    served = _served_worktree(repo, "served-legacy", "next")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=? WHERE id=?", (str(served), tid))
        task = kb.get_task(conn, tid)
        assert task is not None
        ws, branch = kbw._resolve_worktree_workspace(task)
        assert ws != served.resolve() and branch == f"wt/{tid}"
    assert served.is_dir()

def test_legacy_row_claiming_served_branch_cannot_reap_it(kanban_home: Path, repo: Path) -> None:
    served = _served_worktree(repo, "served-claimed", "next")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name='next' WHERE id=?", (str(served), tid))
        assert kb.archive_task(conn, tid)
    assert served.is_dir()


# ---------------------------------------------------------------------------
# Creation-time guard: create_task refuses to adopt someone else's checkout
# ---------------------------------------------------------------------------


def test_create_task_rejects_worktree_path_on_foreign_branch(kanban_home: Path, repo: Path) -> None:
    """(c) worktree kind pointing at an existing checkout on another branch -> ValueError."""
    served = _served_worktree(repo, "served-next-create", "next")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(
                conn, title="x", assignee="worker",
                workspace_kind="worktree", workspace_path=str(served),
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    msg = str(excinfo.value)
    assert "next" in msg
    assert ".worktrees" in msg  # tells the caller to pass a NEW path under <repo>/.worktrees/
    assert served.is_dir()


def test_create_task_rejects_foreign_checkout_even_with_other_branch_name(
    kanban_home: Path, repo: Path
) -> None:
    served = _served_worktree(repo, "served-next-create2", "next")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.create_task(
                conn, title="x", assignee="worker", workspace_kind="worktree",
                workspace_path=str(served), branch_name="wt/something-else",
            )


def test_create_task_cannot_claim_existing_checkout_by_naming_its_branch(kanban_home: Path, repo: Path) -> None:
    """Caller-controlled branch_name is not worktree provenance."""
    wt = _make_worktree(repo, "retry-me", branch="wt/retry-me")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="claim", assignee="worker",
                           workspace_kind="worktree", workspace_path=str(wt), branch_name="wt/retry-me")
    assert wt.is_dir()


def test_create_task_allows_new_path_and_repo_root_anchor(kanban_home: Path, repo: Path) -> None:
    """Paths the dispatcher will create, and a repo-root anchor, are untouched by the guard."""
    fresh = repo / ".worktrees" / "brand-new"
    with kbc.connect_closing() as conn:
        t1 = kb.create_task(conn, title="new", assignee="w", workspace_kind="worktree",
                            workspace_path=str(fresh))
        t2 = kb.create_task(conn, title="anchor", assignee="w", workspace_kind="worktree",
                            workspace_path=str(repo))
        task1, task2 = kb.get_task(conn, t1), kb.get_task(conn, t2)
        assert task1 is not None and task2 is not None
        assert task1.workspace_path == str(fresh)
        assert task2.workspace_path == str(repo)
        ws2 = kbw.resolve_workspace(task2)
    assert ws2 == repo / ".worktrees" / t2


def test_tree_dirtied_between_check_and_removal_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: a tree that becomes dirty after the pre-check is NOT removed.

    Simulates the race by making the pre-check see a clean tree while the
    tree is actually dirty when ``git worktree remove`` runs. Without
    ``--force``, git's own dirty guard re-verifies at removal time and the
    removal fails safe.
    """
    import cli

    wt = _make_worktree(repo, "t_gggg7777")
    (wt / "late-wip.txt").write_text("dirtied after the check\n", encoding="utf-8")
    # Pre-check lies (as if the file appeared just after it ran) — real git
    # must still refuse the removal.
    from hermes_cli import worktree_ops

    monkeypatch.setattr(worktree_ops, "_worktree_is_dirty", lambda _p: False)
    kbw._cleanup_worktree_workspace("t_gggg7777", str(wt))
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()


# ---------------------------------------------------------------------------
# Lifecycle integration: complete / archive / deferred parents
# ---------------------------------------------------------------------------


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def test_complete_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")


def test_complete_task_preserves_dirty_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_archive_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid)
    assert not wt.exists()


def test_parent_worktree_deferred_until_children_done(
    kanban_home: Path, repo: Path
) -> None:
    with kbc.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.complete_task(conn, parent, summary="parent done")
        # child still active -> parent worktree must survive for handoff
        assert parent_wt.is_dir()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    # last child terminal -> deferred parent worktree reaped
    assert not parent_wt.exists()
