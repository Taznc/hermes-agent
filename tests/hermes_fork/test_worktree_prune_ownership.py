"""Fork-owned tests for ``hermes_fork.worktree_prune_ownership`` (anchor ``worktree-prune-ownership``).

Moved out of tests/hermes_cli/test_worktree.py (t_a8d56f3d). The startup pruner only reaps trees
Hermes created (branch prefix AND creation marker); foreign locks are never unlocked; orphan
branches are never deleted.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from hermes_cli import worktree_ops
from hermes_fork import worktree_prune_ownership as prune


@pytest.fixture
def git_repo(tmp_path):
    """Same shape as tests/hermes_cli/test_worktree.py::git_repo (initial commit counts as pushed)."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "test@test.com"], ["config", "user.name", "Test"]):
        subprocess.run(["git", *args], cwd=repo, capture_output=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://example.com/test-repo.git"], cwd=repo, capture_output=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, capture_output=True)
    return repo


def _age(path, hours):
    t = time.time() - (hours * 3600)
    os.utime(path, (t, t))


def _mk_on(repo, name, branch=None, detach=False, age_h=500):
    p = repo / ".worktrees" / name
    (repo / ".worktrees").mkdir(exist_ok=True)
    if detach:
        args = ["git", "worktree", "add", "--detach", str(p), "HEAD"]
    else:
        # Pre-existing long-lived branch, fully "pushed" (same commit as origin/main).
        subprocess.run(["git", "branch", branch, "HEAD"], cwd=repo, capture_output=True)
        args = ["git", "worktree", "add", str(p), branch]
    r = subprocess.run(args, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _age(p, age_h)
    return p


def _mk_locked(repo, name, reason):
    p = repo / ".worktrees" / name
    (repo / ".worktrees").mkdir(exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(p), "-b", f"hermes/{name}", "HEAD"], cwd=repo, capture_output=True)
    subprocess.run(["git", "worktree", "lock", "--reason", reason, str(p)], cwd=repo, capture_output=True)
    return p


def _porcelain(repo) -> str:
    return subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                          check=True, capture_output=True, text=True).stdout


# --------------------------------------------------------------------------------------- the anchor


def test_anchor_rebinds_pruner_functions():
    for name, replacement in prune.REPLACEMENTS.items():
        assert getattr(worktree_ops, name) is replacement, f"worktree-prune-ownership anchor did not rebind {name}"
    for name in prune.WRAPPERS:
        assert getattr(getattr(worktree_ops, name), "__fork_wrapped__", None) is not None, name


def test_install_is_idempotent():
    before = {name: getattr(worktree_ops, name) for name in (*prune.REPLACEMENTS, *prune.WRAPPERS)}
    prune.install(worktree_ops.__name__)
    assert {name: getattr(worktree_ops, name) for name in before} == before


def test_cli_reexports_reach_the_fork_functions(git_repo):
    """cli.py re-exports worktree_ops names at import; the anchor runs before that import finishes,
    so a worktree created through the ``cli`` facade carries the prune-owner marker."""
    import cli
    assert cli._setup_worktree is worktree_ops._setup_worktree
    info = cli._setup_worktree(str(git_repo), sync_base=False)
    assert info is not None
    try:
        assert prune.is_prune_owned_worktree(info["path"], info["branch"])
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", info["path"]], cwd=git_repo, capture_output=True)


# ------------------------------------------------------------------------------ pruner branch ownership


def test_tree_on_trunk_branch_never_reaped(git_repo):
    import cli
    served = _mk_on(git_repo, "served-next", branch="next")
    assert not worktree_ops._worktree_is_dirty(str(served), str(git_repo))
    assert not worktree_ops._worktree_has_unpushed_commits(str(served)), (
        "precondition: every upstream gate passes — only ownership can save this tree"
    )
    cli._prune_stale_worktrees(str(git_repo))
    assert served.exists() and (served / "README.md").exists()
    assert "next" in subprocess.run(["git", "branch", "--list", "next"], cwd=git_repo,
                                    capture_output=True, text=True).stdout


def test_tree_on_user_feature_branch_never_reaped(git_repo):
    import cli
    wt = _mk_on(git_repo, "my-feature", branch="feature/handmade")
    cli._prune_stale_worktrees(str(git_repo))
    assert wt.exists()


def test_hand_managed_tree_with_hermes_prefix_is_preserved(git_repo):
    import cli
    wt = _mk_on(git_repo, "hermes-handmade", branch="hermes/hermes-handmade")
    cli._prune_stale_worktrees(str(git_repo))
    assert wt.exists(), "a branch prefix is not evidence Hermes created this checkout"


def test_hand_managed_orphan_branch_is_not_deleted(git_repo):
    import cli
    subprocess.run(["git", "branch", "hermes/hermes-handmade", "HEAD"], cwd=git_repo, check=True, capture_output=True)
    cli._prune_stale_worktrees(str(git_repo))
    assert subprocess.run(["git", "show-ref", "--verify", "refs/heads/hermes/hermes-handmade"],
                          cwd=git_repo, capture_output=True).returncode == 0


def test_detached_head_tree_never_reaped(git_repo):
    import cli
    wt = _mk_on(git_repo, "pinned", detach=True)
    cli._prune_stale_worktrees(str(git_repo))
    assert wt.exists()


def test_hermes_owned_prefixes_still_reaped(git_repo):
    import cli
    trees = {
        "hermes-scratch": "hermes/hermes-scratch",
        "t_named_kanban": "wt/t_named_kanban",
        "subagent-abc": "hermes-subagent/subagent-abc",
    }
    paths = {n: _mk_on(git_repo, n, branch=b) for n, b in trees.items()}
    for name, path in paths.items():
        prune.mark_prune_owned_worktree(str(path), trees[name])
    cli._prune_stale_worktrees(str(git_repo))
    for name, p in paths.items():
        assert not p.exists(), f"{name} on {trees[name]} is Hermes scratch and should be reaped"


def test_prune_removal_never_forces(git_repo, monkeypatch):
    """Every ``git worktree remove`` the pruner issues omits ``--force``, including for a tree with
    untracked-only content (the case upstream force-removed); git's own dirty guard stays in force."""
    import cli
    clean = _mk_on(git_repo, "hermes-clean", branch="hermes/hermes-clean")
    untracked = _mk_on(git_repo, "hermes-untracked", branch="hermes/hermes-untracked")
    (untracked / "scratch.txt").write_text("notes\n")
    _age(untracked, 500)
    for p, b in ((clean, "hermes/hermes-clean"), (untracked, "hermes/hermes-untracked")):
        prune.mark_prune_owned_worktree(str(p), b)
    calls = []
    real_git = worktree_ops._git

    def spy(args, cwd, *a, **kw):
        calls.append(list(args))
        return real_git(args, cwd, *a, **kw)

    monkeypatch.setattr(worktree_ops, "_git", spy)
    cli._prune_stale_worktrees(str(git_repo))
    removes = [c for c in calls if c[:2] == ["worktree", "remove"]]
    assert removes, "precondition: the pruner attempted removal"
    assert not any("--force" in c or "-f" in c for c in removes), removes
    assert not clean.exists()
    assert untracked.exists() and (untracked / "scratch.txt").exists()


def test_marker_for_other_branch_is_not_ownership(git_repo):
    wt = _mk_on(git_repo, "hermes-moved", branch="hermes/hermes-moved")
    prune.mark_prune_owned_worktree(str(wt), "hermes/something-else")
    assert prune.owned_for_prune(str(wt)) == (False, "hermes/hermes-moved")


def test_lock_added_after_classification_is_preserved(git_repo):
    wt = _mk_on(git_repo, "hermes-lock-race", branch="hermes/hermes-lock-race")
    prune.mark_prune_owned_worktree(str(wt), "hermes/hermes-lock-race")
    subprocess.run(["git", "worktree", "lock", "--reason", "served deployment", str(wt)],
                   cwd=git_repo, check=True, capture_output=True)
    worktree_ops._reap_prune_verdicts(str(git_repo), [(wt, 0, True, "reap", None)], 1)
    assert wt.exists()
    assert "locked served deployment" in _porcelain(git_repo)


def test_reap_rechecks_branch_after_classification(git_repo):
    wt = _mk_on(git_repo, "hermes-race", branch="hermes/hermes-race")
    prune.mark_prune_owned_worktree(str(wt), "hermes/hermes-race")
    # Classification approved the original branch; before serial mutation the checkout changed
    # to a served (fully pushed) branch.
    subprocess.run(["git", "branch", "next", "HEAD"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "switch", "next"], cwd=wt, check=True, capture_output=True)
    worktree_ops._reap_prune_verdicts(str(git_repo), [(wt, 0, True, "reap", None)], 1)
    assert wt.exists() and prune.worktree_symbolic_branch(str(wt)) == "next"


def test_foreign_verdict_reported_when_stale(git_repo):
    wt = _mk_on(git_repo, "served", branch="next")
    verdicts = worktree_ops._classify_prune_candidates(str(git_repo), [(wt, 0, True)])
    assert verdicts == [(wt, 0, True, prune.FOREIGN_VERDICT, None)]
    preserved, kept = worktree_ops._reap_prune_verdicts(str(git_repo), verdicts, 1)
    assert preserved == [f"served ({prune.FOREIGN_REASON})"] and kept == set()
    assert wt.exists()


def test_ownership_predicate(git_repo):
    served = _mk_on(git_repo, "pred-next", branch="next")
    assert prune.worktree_on_hermes_owned_branch(str(served)) == (False, "next")
    detached = _mk_on(git_repo, "pred-detached", detach=True)
    assert prune.worktree_on_hermes_owned_branch(str(detached)) == (False, None)
    mine = _mk_on(git_repo, "pred-wt", branch="wt/pred")
    assert prune.worktree_on_hermes_owned_branch(str(mine)) == (True, "wt/pred")


# ----------------------------------------------------------------------------------------- locks


def test_foreign_lock_on_marked_tree_survives_prune(git_repo):
    import cli
    wt = _mk_on(git_repo, "hermes-deployment", branch="hermes/hermes-deployment")
    prune.mark_prune_owned_worktree(str(wt), "hermes/hermes-deployment")
    subprocess.run(["git", "worktree", "lock", "--reason", "served deployment", str(wt)],
                   cwd=git_repo, check=True, capture_output=True)
    cli._prune_stale_worktrees(str(git_repo))
    assert wt.exists()
    assert "locked served deployment" in _porcelain(git_repo)


@pytest.mark.parametrize("reason", ["some other tool", "served: hermes pid=999999", "hermes pid=12 extra"])
def test_non_hermes_lock_reason_is_live(git_repo, reason):
    p = _mk_locked(git_repo, "hermes-foreign", reason)
    assert worktree_ops._worktree_lock_is_live(str(git_repo), str(p)) == "live"


def test_dead_hermes_pid_lock_is_dead(git_repo):
    p = _mk_locked(git_repo, "hermes-dead", "hermes pid=999999")
    assert worktree_ops._worktree_lock_is_live(str(git_repo), str(p)) == "dead"


# -------------------------------------------------------------------------------------- creation marks


def test_setup_worktree_marks_ownership(git_repo):
    info = worktree_ops._setup_worktree(str(git_repo), sync_base=False)
    assert info is not None
    try:
        assert prune.is_prune_owned_worktree(info["path"], info["branch"])
        assert prune.owned_for_prune(info["path"]) == (True, info["branch"])
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", info["path"]], cwd=git_repo, capture_output=True)


def test_subagent_worktree_marks_ownership(git_repo):
    from tools.subagent_worktree import create_subagent_worktree
    info = create_subagent_worktree(str(git_repo), "abc123")
    assert info is not None
    assert prune.is_prune_owned_worktree(info["path"], info["branch"])


def test_mark_failure_is_logged_not_raised(tmp_path, caplog):
    import logging
    log = logging.getLogger("test.prune")
    with caplog.at_level("WARNING", logger="test.prune"):
        prune.mark_created_worktree(str(tmp_path), "hermes/x", log, "cannot mark %s: %s")
    assert "cannot mark" in caplog.text
