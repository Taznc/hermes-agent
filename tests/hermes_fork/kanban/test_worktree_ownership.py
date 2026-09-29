"""Fork-owned tests for ``hermes_fork.kanban.worktree_ownership`` (anchor ``kanban-worktree-ownership``).

Moved out of tests/hermes_cli/test_kanban_worktree_teardown.py and tests/tools/test_kanban_tools.py
(t_a8d56f3d). Incident 2026-09-27: a task created with ``workspace_kind='worktree'`` and
``workspace_path`` pointing at the SERVED checkout of branch ``next`` (itself a linked worktree of
the main repo) was archived 13s later; that ran ``git worktree remove`` on the tree — every
upstream guard passed (not the main checkout, clean, fully pushed) — and the live services lost
their source. Nothing checked that the tree was the task's own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_fork.kanban import worktree_ownership as own


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


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
    return bool(_git("-C", str(repo), "branch", "--list", branch).strip())


def _served_worktree(repo: Path, name: str, branch: str) -> Path:
    """A linked worktree on a long-lived, fully-pushed branch (like a served ``next``)."""
    _git("-C", str(repo), "branch", branch, "HEAD")
    _git("-C", str(repo), "push", "origin", branch)
    target = repo.parent / name
    _git("-C", str(repo), "worktree", "add", str(target), branch)
    return target


# --------------------------------------------------------------------------------------- the anchor


def test_anchor_rebinds_every_worktree_lifecycle_function() -> None:
    for name, replacement in own.REPLACEMENTS.items():
        assert getattr(kbw, name) is replacement, f"kanban-worktree-ownership anchor did not rebind {name}"


def test_install_is_idempotent() -> None:
    before = {name: getattr(kbw, name) for name in own.REPLACEMENTS}
    own.install(kbw.__name__)
    assert {name: getattr(kbw, name) for name in own.REPLACEMENTS} == before


def test_public_entry_points_route_through_the_guard(kanban_home: Path, repo: Path) -> None:
    """``resolve_workspace`` / ``_cleanup_workspace`` (the upstream public paths) reach the fork
    replacements: resolving marks the new tree and cleanup removes it, while cleanup of a row that
    points at a foreign checkout leaves that checkout alone."""
    served = _served_worktree(repo, "served-public", "next")
    with kbc.connect_closing() as conn:
        own_tid = kb.create_task(conn, title="own", assignee="worker", workspace_kind="worktree",
                                 workspace_path=str(repo / ".worktrees" / "public-new"))
        task = kb.get_task(conn, own_tid)
        assert task is not None
        ws = Path(kbw.resolve_workspace(task))
        assert own.provenance(ws) == (own_tid, f"wt/{own_tid}")
        foreign_tid = kb.create_task(conn, title="foreign", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? "
                         "WHERE id=?", (str(served), "next", foreign_tid))
            conn.execute("UPDATE tasks SET branch_name=? WHERE id=?", (f"wt/{own_tid}", own_tid))
        kbw._cleanup_workspace(conn, foreign_tid)
        assert served.is_dir()
        kbw._cleanup_workspace(conn, own_tid)
        assert not ws.exists()


# ------------------------------------------------------------ cleanup: only the task's own worktree


def test_worktree_on_foreign_branch_preserved_when_task_has_no_branch(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The incident shape: branch_name None -> owned branch is wt/<id>, the tree is on ``next`` ->
    preserved, WARNING names both branches."""
    served = _served_worktree(repo, "served-next", "next")
    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        kbw._cleanup_worktree_workspace("t_0b571cee", str(served), None)
    assert served.is_dir(), "a clean, pushed tree on another branch must survive archive"
    assert (served / "README.md").exists()
    assert _branch_exists(repo, "next")
    assert f"worktree {served.resolve()}" in _git("-C", str(repo), "worktree", "list", "--porcelain")
    msg = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "next" in msg and "wt/t_0b571cee" in msg, msg


def test_worktree_on_foreign_branch_preserved_when_task_names_another_branch(repo: Path) -> None:
    served = _served_worktree(repo, "served-main-alias", "release/1.0")
    kbw._cleanup_worktree_workspace("t_dddd0001", str(served), "wt/t_dddd0001")
    assert served.is_dir()
    assert _branch_exists(repo, "release/1.0")


def test_detached_head_worktree_preserved(repo: Path) -> None:
    target = repo.parent / "detached"
    _git("-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD")
    kbw._cleanup_worktree_workspace("t_dddd0002", str(target), None)
    assert target.is_dir()


def test_unmarked_tree_on_the_tasks_own_branch_preserved(repo: Path) -> None:
    """Branch equality alone is not ownership: no dispatch-time marker -> preserved."""
    target = repo / ".worktrees" / "t_dddd0003"
    _git("-C", str(repo), "worktree", "add", "-b", "wt/t_dddd0003", str(target), "HEAD")
    kbw._cleanup_worktree_workspace("t_dddd0003", str(target), None)
    assert target.is_dir()
    assert _branch_exists(repo, "wt/t_dddd0003")


def test_marker_for_another_task_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd0004", branch="wt/shared")
    kbw._cleanup_worktree_workspace("t_dddd0005", str(wt), "wt/shared")
    assert wt.is_dir()


def test_worktree_on_own_wt_branch_still_removed(repo: Path) -> None:
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


def test_only_the_default_task_branch_is_deleted(repo: Path) -> None:
    """Upstream deleted any ``wt/*`` branch; the fork deletes only ``wt/<task-id>``."""
    wt = _make_worktree(repo, "t_bbbb0003", branch="wt/shared-lane")
    kbw._cleanup_worktree_workspace("t_bbbb0003", str(wt), "wt/shared-lane")
    assert not wt.exists()
    assert _branch_exists(repo, "wt/shared-lane")


def test_ownership_rechecked_right_before_removal(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkout switches branch between the first ownership check and ``git worktree remove``."""
    wt = _make_worktree(repo, "t_bbbb0004")
    _git("-C", str(repo), "branch", "next", "HEAD")
    real_release = kbw.release_lsp_clients

    def switch_then_release(path: str) -> None:
        _git("-C", str(wt), "switch", "next")
        real_release(path)

    monkeypatch.setattr(kbw, "release_lsp_clients", switch_then_release)
    kbw._cleanup_worktree_workspace("t_bbbb0004", str(wt), None)
    assert wt.is_dir()
    assert _git("-C", str(wt), "symbolic-ref", "--short", "HEAD").strip() == "next"


def test_marker_lives_in_git_admin_dir_not_the_working_tree(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb0005")
    assert own.provenance(wt) == ("t_bbbb0005", "wt/t_bbbb0005")
    assert not (wt / own.PROVENANCE_FILE).exists()
    assert _git("-C", str(wt), "status", "--porcelain").strip() == ""
    admin = Path(_git("-C", str(wt), "rev-parse", "--path-format=absolute", "--git-dir").strip())
    assert json.loads((admin / own.PROVENANCE_FILE).read_text(encoding="utf-8")) == {
        "task_id": "t_bbbb0005", "branch": "wt/t_bbbb0005",
    }


def test_archive_task_preserves_served_checkout_on_other_branch(kanban_home: Path, repo: Path) -> None:
    """End-to-end replay of the incident through archive_task -> _cleanup_workspace. The DB row is
    written directly (as the incident row was) to bypass the creation-time guard and prove the
    cleanup guard alone is sufficient."""
    served = _served_worktree(repo, "served-next-e2e", "next")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="chat-created", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=NULL WHERE id=?",
                (str(served), tid),
            )
        assert kb.archive_task(conn, tid)
    assert served.is_dir()
    assert (served / "README.md").exists()
    assert _branch_exists(repo, "next")


def test_legacy_row_claiming_served_branch_cannot_reap_it(kanban_home: Path, repo: Path) -> None:
    served = _served_worktree(repo, "served-claimed", "next")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name='next' WHERE id=?",
                         (str(served), tid))
        assert kb.archive_task(conn, tid)
    assert served.is_dir()


# ------------------------------------------------------------------ dispatch: adopt / fall back / refuse


def test_existing_unowned_checkout_is_never_adopted(repo: Path) -> None:
    target = repo / ".worktrees" / "t_cccc0001"
    _git("-C", str(repo), "worktree", "add", "-b", "wt/t_cccc0001", str(target), "HEAD")
    with pytest.raises(ValueError, match="unowned worktree"):
        kbw._ensure_git_worktree(repo, target, "wt/t_cccc0001", "t_cccc0001")
    assert own.provenance(target) is None


def test_own_marked_checkout_is_reused(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc0002")
    kbw._ensure_git_worktree(repo, wt, "wt/t_cccc0002", "t_cccc0002")  # no raise, no re-add
    assert own.provenance(wt) == ("t_cccc0002", "wt/t_cccc0002")


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
        assert own.provenance(ws) == (tid, f"wt/{tid}")
    assert served.is_dir()


def test_legacy_canonical_resume_preserves_unmarked_tree(kanban_home: Path, repo: Path) -> None:
    """An old worker-spawn record plus canonical Git checkout permits resume, not ownership."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="old worker", assignee="worker")
        wt = repo / ".worktrees" / tid
        _git("-C", str(repo), "worktree", "add", "-b", f"wt/{tid}", str(wt), "HEAD")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? WHERE id=?",
                         (str(wt), f"wt/{tid}", tid))
            conn.execute("INSERT INTO task_runs (task_id, status, started_at, worker_pid) VALUES (?, 'released', 1, 12345)",
                         (tid,))
        task = kb.get_task(conn, tid)
        assert kbw._resolve_worktree_workspace(task, conn=conn) == (wt.resolve(), f"wt/{tid}")
        assert own.provenance(wt) is None
        kbw._cleanup_worktree_workspace(tid, str(wt), f"wt/{tid}")
        assert wt.exists(), "legacy resume never grants teardown ownership"


def test_legacy_canonical_without_prior_worker_is_not_adopted(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="forged row", assignee="worker")
        wt = repo / ".worktrees" / tid
        _git("-C", str(repo), "worktree", "add", "-b", f"wt/{tid}", str(wt), "HEAD")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? WHERE id=?",
                         (str(wt), f"wt/{tid}", tid))
        with pytest.raises(ValueError, match="unowned worktree"):
            kbw._resolve_worktree_workspace(kb.get_task(conn, tid), conn=conn)
        assert wt.exists() and own.provenance(wt) is None


def test_legacy_noncanonical_prior_worker_does_not_fallback(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="old custom", assignee="worker")
        wt = repo / ".worktrees" / "old-custom"
        _git("-C", str(repo), "worktree", "add", "-b", f"wt/{tid}", str(wt), "HEAD")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? WHERE id=?",
                         (str(wt), f"wt/{tid}", tid))
            conn.execute("INSERT INTO task_runs (task_id, status, started_at, worker_pid) VALUES (?, 'released', 1, 12345)",
                         (tid,))
        with pytest.raises(ValueError, match="unowned worktree"):
            kbw._resolve_worktree_workspace(kb.get_task(conn, tid), conn=conn)
        assert wt.exists() and not (repo / ".worktrees" / tid).exists()


def _legacy_canonical_task(conn, repo: Path, *, prior_worker: bool) -> tuple[str, Path]:
    """A ready worktree task whose canonical ``<repo>/.worktrees/<id>`` checkout predates markers."""
    tid = kb.create_task(conn, title="legacy dispatch", assignee="worker")
    wt = repo / ".worktrees" / tid
    _git("-C", str(repo), "worktree", "add", "-b", f"wt/{tid}", str(wt), "HEAD")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? WHERE id=?",
                     (str(wt), f"wt/{tid}", tid))
        if prior_worker:
            conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, worker_pid) "
                         "VALUES (?, 'released', 1, 2, 12345)", (tid,))
    return tid, wt


@pytest.mark.parametrize("prior_worker", [True, False], ids=["prior-worker-resumes", "no-prior-worker-refused"])
def test_dispatch_resumes_legacy_canonical_worktree_only_after_a_prior_worker(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, prior_worker: bool,
) -> None:
    """Through the real dispatcher tick: a legacy unmarked canonical checkout is resumed (spawned in
    place, still unmarked, so teardown stays disabled) only when an earlier worker ran there;
    otherwise the claim fails with ``spawn_failed`` and nothing is spawned or marked."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    spawned: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append((task.id, workspace))
        return None

    with kbc.connect_closing() as conn:
        tid, wt = _legacy_canonical_task(conn, repo, prior_worker=prior_worker)
        result = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        assert task is not None
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,))]

    assert wt.is_dir() and own.provenance(wt) is None
    if prior_worker:
        assert spawned == [(tid, str(wt.resolve()))]
        assert [s[0] for s in result.spawned] == [tid]
        assert task.status == "running" and task.workspace_path == str(wt.resolve())
        kbw._cleanup_worktree_workspace(tid, str(wt), f"wt/{tid}")
        assert wt.is_dir(), "a resumed legacy tree never gains teardown ownership"
    else:
        assert spawned == [] and not result.spawned
        assert task.status != "running"
        assert outcomes[-1] == "spawn_failed"
        assert not any((repo / ".worktrees").glob(f"{tid}?*")), "no fallback tree is created"


# ------------------------------------------------------------------ creation: never adopt a checkout


def test_create_task_rejects_worktree_path_on_foreign_branch(kanban_home: Path, repo: Path) -> None:
    served = _served_worktree(repo, "served-next-create", "next")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="x", assignee="worker",
                           workspace_kind="worktree", workspace_path=str(served))
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    msg = str(excinfo.value)
    assert "next" in msg
    assert ".worktrees" in msg  # tells the caller to pass a NEW path under <repo>/.worktrees/
    assert served.is_dir()


def test_create_task_rejects_foreign_checkout_even_with_other_branch_name(kanban_home: Path, repo: Path) -> None:
    served = _served_worktree(repo, "served-next-create2", "next")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="x", assignee="worker", workspace_kind="worktree",
                           workspace_path=str(served), branch_name="wt/something-else")


def test_create_task_cannot_claim_existing_checkout_by_naming_its_branch(kanban_home: Path, repo: Path) -> None:
    """Caller-controlled branch_name is not worktree provenance."""
    wt = _make_worktree(repo, "retry-me", branch="wt/retry-me")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="claim", assignee="worker",
                           workspace_kind="worktree", workspace_path=str(wt), branch_name="wt/retry-me")
    assert wt.is_dir()


def test_create_task_allows_new_path_and_repo_root_anchor(kanban_home: Path, repo: Path) -> None:
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


def test_create_task_ignores_non_worktree_kinds(kanban_home: Path, repo: Path) -> None:
    served = _served_worktree(repo, "served-dir-kind", "next")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="dir", assignee="w", workspace_kind="dir", workspace_path=str(served))
        assert kb.get_task(conn, tid).workspace_path == str(served)


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Mirror of tests/tools/test_kanban_tools.py::worker_env: HERMES_HOME isolated, a claimed task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def test_kanban_create_tool_refuses_worktree_path_on_foreign_checkout(
    monkeypatch: pytest.MonkeyPatch, worker_env, tmp_path: Path
) -> None:
    """Incident replay at the tool seam: ``kanban_create`` with ``workspace_kind='worktree'``
    pointing at a pre-existing linked checkout on another branch is rejected as a structured tool
    error naming the branch and a fresh ``<repo>/.worktrees/<name>`` path."""
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    def git(*args, cwd):
        r = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@x", *args],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    project = tmp_path / "repo"
    project.mkdir()
    git("init", "-b", "main", cwd=project)
    (project / "README.md").write_text("x\n")
    git("add", "README.md", cwd=project)
    git("commit", "-q", "-m", "init", cwd=project)
    git("branch", "next", cwd=project)
    served = tmp_path / "repo-next"
    git("worktree", "add", str(served), "next", cwd=project)

    out = json.loads(kt._handle_create({
        "title": "port X", "assignee": "coder",
        "workspace_kind": "worktree", "workspace_path": str(served),
    }))
    assert out.get("ok") is not True, out
    err = out.get("error") or json.dumps(out)
    assert "next" in err and ".worktrees" in err, err
    assert served.is_dir() and (served / "README.md").exists()
    conn = kbc.connect()
    try:
        assert all(t.title != "port X" for t in kb.list_tasks(conn))
    finally:
        conn.close()

    ok = json.loads(kt._handle_create({
        "title": "port X", "assignee": "coder",
        "workspace_kind": "worktree", "workspace_path": str(project / ".worktrees" / "port-x"),
    }))
    assert ok["ok"] is True, ok
