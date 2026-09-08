"""``hermes kanban land`` — attended, fail-closed landing of an approved review.

Every test builds a real temporary git repo + bare remote and a temporary
Kanban DB; nothing here touches the developer's own repo or board.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_approve as ka
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_land as kl


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Real git fixtures: a bare "remote", a clone, and a task worktree
# ---------------------------------------------------------------------------


def git(cwd, *args, check=True):
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={
            "HOME": str(cwd), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Repo:
    """A bare remote + a working clone + per-task worktrees."""

    def __init__(self, root: Path):
        self.root = root
        self.remote = root / "remote.git"
        self.clone = root / "clone"
        git(root.parent if root.exists() else root, "init", "--bare", "-b", "dev", str(self.remote))
        git(root, "clone", str(self.remote), str(self.clone))
        # Repo-local identity: the production code path merges with the
        # operator's own git identity, and a worktree inherits repo config.
        git(self.clone, "config", "user.name", "T")
        git(self.clone, "config", "user.email", "t@example.invalid")
        write(self.clone / "README.md", "base\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-m", "base")
        git(self.clone, "push", "origin", "HEAD:refs/heads/dev")
        git(self.clone, "branch", "--set-upstream-to=origin/dev", "dev")

    def task_worktree(self, task_id: str, *, content: str = "feature\n") -> Path:
        branch = f"wt/{task_id}"
        path = self.clone / ".worktrees" / task_id
        git(self.clone, "worktree", "add", "-b", branch, str(path), "origin/dev")
        write(path / f"{task_id}.txt", content)
        git(path, "add", "-A")
        git(path, "commit", "-m", f"work for {task_id}")
        git(path, "push", "origin", f"HEAD:refs/heads/{branch}")
        git(path, "branch", f"--set-upstream-to=origin/{branch}", branch)
        return path

    def remote_sha(self, branch: str) -> str:
        out = git(self.clone, "ls-remote", str(self.remote), f"refs/heads/{branch}")
        return out.split("\t")[0] if out else ""


@pytest.fixture
def repo(tmp_path) -> Repo:
    root = tmp_path / "git"
    root.mkdir()
    return Repo(root)


def make_approved_task(conn, repo: Repo, **worktree_kw):
    """An approved card whose workspace is a real pushed task worktree.

    Approval goes through the explicit reviewer verdict, NOT ``complete_task``:
    completion reaps the worktree, and the whole point of the landing command
    is that the reviewed tree survives until the content is proven on the
    remote.
    """
    task_id = kb.create_task(conn, title="impl", assignee="dev-a")
    path = repo.task_worktree(task_id, **worktree_kw)
    conn.execute(
        "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, branch_name = ? "
        "WHERE id = ?", (str(path), f"wt/{task_id}", task_id),
    )
    conn.commit()
    _hand_to_review(conn, task_id, summary="impl done")
    assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
    ok, reason = ka.approve_review_task(
        conn, task_id, source_sha=git(path, "rev-parse", "HEAD"), summary="approved",
        metadata={"pre_review_gate": {"pushed": git(path, "rev-parse", "HEAD")}},
    )
    assert ok, reason
    return task_id, path




# ---------------------------------------------------------------------------
# Target resolution — never guessed, only explicit
# ---------------------------------------------------------------------------


def test_target_resolution_refuses_when_nothing_is_configured(kanban_home):
    """No ``--target`` and no board ``land_target``: refuse rather than pick a
    remote. Guessing between a fork and an upstream is the failure this
    command exists to prevent."""
    with pytest.raises(kl.LandRefusal) as exc:
        kl.resolve_target(None, board=None)
    assert exc.value.reason == "no_target"


def test_target_resolution_prefers_explicit_flag_over_board_config(kanban_home):
    kb.write_board_metadata(None, land_target="origin/dev")
    assert kl.resolve_target("otherremote/main", board=None) == ("otherremote", "main")


def test_target_resolution_falls_back_to_board_configuration(kanban_home):
    kb.write_board_metadata(None, land_target="origin/dev")
    assert kl.resolve_target(None, board=None) == ("origin", "dev")


def test_target_resolution_rejects_a_bare_branch_name(kanban_home):
    """``dev`` alone is ambiguous across remotes — the operator must say which."""
    with pytest.raises(kl.LandRefusal) as exc:
        kl.resolve_target("dev", board=None)
    assert exc.value.reason == "target_unresolvable"


# ---------------------------------------------------------------------------
# Approval verdict — read from the review lifecycle, never from ``status``
# ---------------------------------------------------------------------------


def _review_cycle(conn, *, approve: bool, request_changes_after: bool = False,
                  source_sha: str = "0" * 40) -> str:
    """Drive a task through the REAL review lifecycle and return its id."""
    task_id = kb.create_task(conn, title="impl", assignee="dev-a")
    _hand_to_review(conn, task_id, summary="done")
    assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
    if approve:
        ok, reason = ka.approve_review_task(
            conn, task_id, source_sha=source_sha, summary="approved by reviewer",
        )
        assert ok, reason
    if request_changes_after:
        # Approval leaves the card in ``review``; a second look sends it back.
        assert kb.reopen_review_task(conn, task_id)
        _hand_to_review(conn, task_id, summary="again")
        assert kb.claim_review_task(conn, task_id, claimer="lock-rev2") is not None
        ok, _ = kb.request_changes(conn, task_id, reason="needs work")
        assert ok
    return task_id


def _hand_to_review(conn, task_id: str, *, summary: str) -> None:
    """Implementer claims the task and hands it to ``reviewer``, proving run
    ownership the way a real worker does."""
    task = kb.claim_task(conn, task_id, claimer=f"lock-impl-{task_id}")
    assert task is not None
    assert kb.request_review(
        conn, task_id, summary=summary, reviewer="reviewer",
        expected_run_id=task.current_run_id,
    )


def test_approval_requires_a_reviewer_run_not_merely_done_status(kanban_home):
    """A task marked ``done`` by its own implementer never entered review, so it
    carries no approval verdict — landing must refuse it."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="self-completed", assignee="dev-a")
        kb.claim_task(conn, task_id, claimer="lock-impl")
        assert kb.complete_task(conn, task_id, summary="I think it is fine")

        with pytest.raises(kl.LandRefusal) as exc:
            kl.approval_verdict(conn, task_id)
    assert exc.value.reason == "no_approval"


def test_approval_verdict_reads_the_explicit_reviewer_approval(kanban_home):
    with kbc.connect() as conn:
        task_id = _review_cycle(conn, approve=True, source_sha="a" * 40)
        verdict = kl.approval_verdict(conn, task_id)
    assert verdict.run_id is not None
    assert verdict.reviewer == "reviewer"
    assert verdict.summary == "approved by reviewer"
    assert verdict.approved_sha == "a" * 40


def test_approval_refuses_while_review_is_still_open(kanban_home):
    """Card handed to review but not yet approved: no verdict exists yet."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="in review", assignee="dev-a")
        _hand_to_review(conn, task_id, summary="done")

        with pytest.raises(kl.LandRefusal) as exc:
            kl.approval_verdict(conn, task_id)
    assert exc.value.reason == "no_approval"


def test_approval_is_invalidated_by_a_later_request_changes(kanban_home):
    """An older approval must not outrank a newer ``changes_requested``."""
    with kbc.connect() as conn:
        task_id = _review_cycle(conn, approve=True, request_changes_after=True)

        with pytest.raises(kl.LandRefusal) as exc:
            kl.approval_verdict(conn, task_id)
    assert exc.value.reason == "changes_requested_unresolved"


def test_a_stale_approval_cannot_authorize_a_newer_open_review(kanban_home):
    """The card was approved, then re-submitted for review without anyone
    adjudicating the new cycle. The old verdict describes a cycle that is over;
    treating it as live would land work no reviewer ever looked at."""
    with kbc.connect() as conn:
        task_id = _review_cycle(conn, approve=True, source_sha="a" * 40)
        assert kb.reopen_review_task(conn, task_id)
        _hand_to_review(conn, task_id, summary="round two")

        with pytest.raises(kl.LandRefusal) as exc:
            kl.approval_verdict(conn, task_id)
    assert exc.value.reason == "approval_superseded"


def test_landing_refuses_when_the_branch_moved_since_it_was_approved(kanban_home, repo):
    """Approval binds to ONE commit. A branch advanced after the reviewer read
    it must not land on the strength of that reading — verification is not
    review, so even a passing verify command cannot rescue this."""
    kb.write_board_metadata(None, land_verify="true")
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        approved_sha = repo.remote_sha(f"wt/{task_id}")
        write(path / "sneaked-in.txt", "never reviewed\n")
        git(path, "add", "-A")
        git(path, "commit", "-m", "unreviewed advance")
        git(path, "push", "origin", f"HEAD:refs/heads/wt/{task_id}")
        assert repo.remote_sha(f"wt/{task_id}") != approved_sha

        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
    assert exc.value.reason == "approval_sha_drift"
    git(repo.clone, "fetch", "origin", "dev")
    assert "sneaked-in.txt" not in git(
        repo.clone, "ls-tree", "--name-only", "origin/dev",
    ).splitlines()


# ---------------------------------------------------------------------------
# Source state — the pushed branch is the source of truth
# ---------------------------------------------------------------------------


def test_source_state_uses_the_pushed_branch_and_reports_its_sha(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        source = kl.source_state(conn, task_id, remote="origin")
    assert source.branch == f"wt/{task_id}"
    assert source.sha == repo.remote_sha(f"wt/{task_id}")
    assert source.remote == "origin"


def test_source_state_refuses_an_unpushed_branch(kanban_home, repo):
    """Nothing to land: the endpoint does not publish the branch at all.

    Note this is checked at LAND time, not review time. The worker-preservation
    net commits and pushes on the review handoff, so a card reaches approval
    published; a branch can still disappear afterwards (deleted by hand, or a
    remote that never had it), and landing must catch that rather than assume
    what was true at review is still true.
    """
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        git(repo.clone, "push", "origin", f":refs/heads/wt/{task_id}")
        assert repo.remote_sha(f"wt/{task_id}") == ""

        with pytest.raises(kl.LandRefusal) as exc:
            kl.source_state(conn, task_id, remote="origin")
    assert exc.value.reason == "branch_unpushed"


def test_source_state_refuses_a_dirty_surviving_worktree(kanban_home, repo):
    """Uncommitted work in the task worktree means the pushed sha is not the
    whole change — refuse rather than land a partial diff."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        # Approval preserves the tree, so it is still here to be dirtied.
        assert path.is_dir(), "an approved worktree must survive until landing"
        write(path / "scratch.txt", "uncommitted\n")

        with pytest.raises(kl.LandRefusal) as exc:
            kl.source_state(conn, task_id, remote="origin")
    assert exc.value.reason == "dirty_worktree"


def test_source_state_refuses_a_worktree_with_commits_ahead_of_the_push(kanban_home, repo):
    """Local commits made after the push would be silently dropped."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        write(path / "later.txt", "after the push\n")
        git(path, "add", "-A")
        git(path, "commit", "-m", "unpushed follow-up")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.source_state(conn, task_id, remote="origin")
    assert exc.value.reason == "branch_unpushed"


def test_source_state_refuses_a_remote_that_does_not_hold_the_branch(kanban_home, repo):
    """Landing to a remote the work was never pushed to (the upstream-vs-fork
    mistake) is refused without naming any specific vendor."""
    git(repo.root, "init", "--bare", "-b", "dev", str(repo.root / "other.git"))
    git(repo.clone, "remote", "add", "elsewhere", str(repo.root / "other.git"))
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        with pytest.raises(kl.LandRefusal) as exc:
            kl.source_state(conn, task_id, remote="elsewhere")
    assert exc.value.reason == "wrong_remote"


def test_source_state_refuses_a_live_worker(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'someone' WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        with pytest.raises(kl.LandRefusal) as exc:
            kl.source_state(conn, task_id, remote="origin")
    assert exc.value.reason == "live_worker"


# ---------------------------------------------------------------------------
# Verification contract — fails closed when missing or stale
# ---------------------------------------------------------------------------


def test_verification_runs_the_board_configured_command_in_the_worktree(kanban_home, repo):
    kb.write_board_metadata(None, land_verify="test -f README.md")
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        source = kl.source_state(conn, task_id, remote="origin")
        receipt = kl.verify(conn, task_id, source, board=None)
    assert receipt["kind"] == "command"
    assert receipt["ok"] is True
    assert receipt["command"] == "test -f README.md"


def test_verification_fails_closed_when_the_configured_command_fails(kanban_home, repo):
    kb.write_board_metadata(None, land_verify="exit 3")
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        source = kl.source_state(conn, task_id, remote="origin")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.verify(conn, task_id, source, board=None)
    assert exc.value.reason == "verification_failed"


def test_verification_falls_back_to_the_receipt_on_the_approval_run(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        source = kl.source_state(conn, task_id, remote="origin")
        receipt = kl.verify(conn, task_id, source, board=None)
    assert receipt["kind"] == "receipt"
    assert receipt["sha"] == source.sha


def _rewrite_approval_metadata(conn, task_id: str, **changes) -> None:
    """Edit the approval run's metadata in place, keeping ``approved_sha`` — the
    approval binding and the verification receipt are separate pieces of
    evidence, and these tests are about the receipt only."""
    row = conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ? AND outcome = 'approved' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    metadata = {**kb._json_dict(row["metadata"]), **changes}
    for key, value in list(metadata.items()):
        if value is None:
            metadata.pop(key)
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?", (json.dumps(metadata), row["id"]),
    )
    conn.commit()


def test_verification_refuses_when_no_receipt_and_no_command_exist(kanban_home, repo):
    """An approval with no verification evidence at all must not land."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        _rewrite_approval_metadata(conn, task_id, pre_review_gate=None)
        source = kl.source_state(conn, task_id, remote="origin")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.verify(conn, task_id, source, board=None)
    assert exc.value.reason == "verification_missing"


def test_verification_refuses_a_receipt_naming_a_different_sha(kanban_home, repo):
    """A receipt from an earlier commit does not vouch for what would land."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        _rewrite_approval_metadata(conn, task_id, pre_review_gate={"pushed": "0" * 40})
        source = kl.source_state(conn, task_id, remote="origin")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.verify(conn, task_id, source, board=None)
    assert exc.value.reason == "verification_stale"


# ---------------------------------------------------------------------------
# land_task — the full attended path, against a real bare remote
# ---------------------------------------------------------------------------


def test_dry_run_reports_a_verdict_and_mutates_nothing(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        before_target = repo.remote_sha("dev")
        result = kl.land_task(conn, task_id, target=("origin", "dev"), dry_run=True)
        status_after = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
    assert result["verdict"] == "would_land"
    assert result["remote"] == "origin" and result["branch"] == "dev"
    assert result["source_sha"] == repo.remote_sha(f"wt/{task_id}")
    assert repo.remote_sha("dev") == before_target, "dry run must not move the target"
    assert status_after == "review", "dry run must not close the card"


def test_landing_merges_pushes_and_reads_the_content_back(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        result = kl.land_task(conn, task_id, target=("origin", "dev"))
        task = kb.get_task(conn, task_id)
    assert result["verdict"] == "landed"
    assert result["target_sha"] == repo.remote_sha("dev")
    assert result["pushed"] is True
    assert result["readback"] == "ancestor"
    # The reviewed content is genuinely reachable on the remote branch now.
    git(repo.clone, "fetch", "origin", "dev")
    files = git(repo.clone, "ls-tree", "--name-only", "origin/dev")
    assert f"{task_id}.txt" in files.splitlines()
    assert task.status == "archived"


def test_landing_records_a_receipt_comment_on_the_card(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        result = kl.land_task(conn, task_id, target=("origin", "dev"))
        bodies = "\n".join(c.body for c in kb.list_comments(conn, task_id))
    for expected in (result["source_sha"], result["target_sha"], "origin", "dev"):
        assert expected in bodies, f"receipt must record {expected!r}"


def test_final_receipt_durably_records_observed_cleanup_after_reopening_db(kanban_home, repo):
    """The audit record must describe completed bookkeeping, not the plan that
    existed before ``complete_task`` ran its best-effort workspace cleanup."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        result = kl.land_task(conn, task_id, target=("origin", "dev"))

    assert result["cleanup"] == {
        "workspace_path": str(path),
        "workspace_removed": True,
        "card_archived": True,
    }

    # Reopen the database so no in-memory ``result`` mutation can satisfy the
    # assertions: all three durable audit surfaces must carry the final facts.
    with kbc.connect() as conn:
        completed = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        receipt_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'landing_receipt' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        comments = kb.list_comments(conn, task_id)
        task = kb.get_task(conn, task_id)

    assert completed is not None
    assert receipt_event is not None
    assert task is not None
    run_receipt = json.loads(completed["metadata"])["landing"]
    event_receipt = json.loads(receipt_event["payload"])["landing"]
    assert run_receipt == event_receipt
    assert run_receipt["cleanup"] == result["cleanup"]
    assert task.status == "archived"
    assert any("Workspace removed: yes" in c.body for c in comments)
    assert any("Card archived: yes" in c.body for c in comments)


def test_landing_refuses_when_the_target_advanced_with_a_conflict(kanban_home, repo):
    """Someone else changed the same file on the target between review and land."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        # A conflicting commit lands on the target first.
        other = repo.clone / ".worktrees" / "other"
        git(repo.clone, "worktree", "add", "--detach", str(other), "origin/dev")
        write(other / f"{task_id}.txt", "someone else's version\n")
        git(other, "add", "-A")
        git(other, "commit", "-m", "conflicting change")
        git(other, "push", "origin", "HEAD:refs/heads/dev")

        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
    assert exc.value.reason == "merge_conflict"


def test_landing_absorbs_a_non_conflicting_target_advance(kanban_home, repo):
    """A target that moved on unrelated files still lands, via a real merge."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        other = repo.clone / ".worktrees" / "other"
        git(repo.clone, "worktree", "add", "--detach", str(other), "origin/dev")
        write(other / "unrelated.txt", "meanwhile\n")
        git(other, "add", "-A")
        git(other, "commit", "-m", "unrelated advance")
        git(other, "push", "origin", "HEAD:refs/heads/dev")

        result = kl.land_task(conn, task_id, target=("origin", "dev"))
    git(repo.clone, "fetch", "origin", "dev")
    files = git(repo.clone, "ls-tree", "--name-only", "origin/dev").splitlines()
    assert result["verdict"] == "landed"
    assert f"{task_id}.txt" in files and "unrelated.txt" in files


def test_landing_is_idempotent_for_already_merged_work(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        first = kl.land_task(conn, task_id, target=("origin", "dev"))
        after_first = repo.remote_sha("dev")
        second = kl.land_task(conn, task_id, target=("origin", "dev"))
    assert first["verdict"] == "landed"
    assert second["verdict"] == "already_landed"
    assert repo.remote_sha("dev") == after_first, "re-run must not merge twice"


def test_idempotent_rerun_does_not_duplicate_the_final_receipt(kanban_home, repo):
    """A finalized re-run must not duplicate the durable bookkeeping surfaces."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        kl.land_task(conn, task_id, target=("origin", "dev"))
        kl.land_task(conn, task_id, target=("origin", "dev"))
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'landing_receipt'",
            (task_id,),
        ).fetchone()[0]
        comment_count = conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ? AND author = 'kanban land'",
            (task_id,),
        ).fetchone()[0]

    assert event_count == 1
    assert comment_count == 1


def test_landing_recognizes_squash_equivalent_work_as_already_landed(kanban_home, repo):
    """The content is on the target as a squashed commit, so the branch sha is
    not an ancestor — patch equivalence must still count as landed."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        other = repo.clone / ".worktrees" / "squash"
        git(repo.clone, "worktree", "add", "--detach", str(other), "origin/dev")
        git(other, "merge", "--squash", f"origin/wt/{task_id}")
        git(other, "commit", "-m", f"squashed {task_id}")
        git(other, "push", "origin", "HEAD:refs/heads/dev")
        after_squash = repo.remote_sha("dev")

        result = kl.land_task(conn, task_id, target=("origin", "dev"))
    assert result["verdict"] == "already_landed"
    assert result["readback"] == "patch_equivalent"
    assert repo.remote_sha("dev") == after_squash


def test_landing_refuses_when_the_push_is_rejected(kanban_home, repo, monkeypatch):
    """A protected branch rejecting the push must not close or clean the card."""
    real_git = kl.git

    def fake_git(cwd, *args, **kw):
        if args and args[0] == "push":
            raise kl.GitError("remote rejected: protected branch")
        return real_git(cwd, *args, **kw)

    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        before = repo.remote_sha("dev")
        monkeypatch.setattr(kl, "git", fake_git)
        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
        monkeypatch.undo()
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
    assert exc.value.reason == "push_rejected"
    assert repo.remote_sha("dev") == before
    assert status == "review", "a rejected push must leave the card open"
    assert path.is_dir(), "a rejected push must leave the reviewed worktree intact"


def test_an_approved_card_keeps_its_worktree_until_landing_closes_it(kanban_home, repo):
    """The end-to-end shape of the safety model in one test: the reviewed tree
    survives approval and every pre-merge gate, and is reaped only once the
    remote read-back has proven the content is there."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        branch = f"wt/{task_id}"

        # Approved, and still fully intact: card open, tree on disk, branch live.
        assert kb.get_task(conn, task_id).status == "review"
        assert path.is_dir()
        assert repo.remote_sha(branch) != ""

        # A dry run changes none of that.
        kl.land_task(conn, task_id, target=("origin", "dev"), dry_run=True)
        assert path.is_dir()
        assert kb.get_task(conn, task_id).status == "review"

        result = kl.land_task(conn, task_id, target=("origin", "dev"))
        task = kb.get_task(conn, task_id)

    assert result["verdict"] == "landed"
    assert task.status == "archived"
    assert not path.exists(), "cleanup runs only after the read-back proved the landing"
    assert result["cleanup"]["workspace_removed"] is True
    # The auto-generated wt/ branch goes with it (existing cleanup seam).
    assert branch not in git(repo.clone, "branch", "--list", branch)


# ---------------------------------------------------------------------------
# CLI surface — `hermes kanban land`, batch isolation, --json
# ---------------------------------------------------------------------------


def run_land(*tokens) -> tuple[str, int]:
    """Drive the real CLI entry point; returns (stdout, exit code)."""
    import contextlib
    import io

    from hermes_cli import kanban as kc

    buf = io.StringIO()
    parser = argparse.ArgumentParser()
    kanban_parser = kc.build_parser(parser.add_subparsers(dest="_top"))
    args = kanban_parser.parse_args(["land", *tokens])
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = kc.kanban_command(args)
    return buf.getvalue(), rc


def test_cli_dry_run_json_emits_a_per_task_verdict_and_mutates_nothing(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
    before = repo.remote_sha("dev")

    out, rc = run_land(task_id, "--target", "origin/dev", "--dry-run", "--json")
    payload = json.loads(out)

    assert rc == 0
    assert [r["task_id"] for r in payload] == [task_id]
    assert payload[0]["verdict"] == "would_land"
    assert payload[0]["remote"] == "origin" and payload[0]["branch"] == "dev"
    assert repo.remote_sha("dev") == before


def test_cli_refuses_without_a_configured_or_explicit_target(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
    out, rc = run_land(task_id, "--dry-run", "--json")
    payload = json.loads(out)
    assert rc != 0
    assert payload[0]["verdict"] == "refused"
    assert payload[0]["reason"] == "no_target"


def test_cli_batch_isolates_one_refusal_from_the_others(kanban_home, repo):
    """A refusal on one card must not abort or misreport its siblings."""
    with kbc.connect() as conn:
        good_id, _ = make_approved_task(conn, repo)
        bad_id = kb.create_task(conn, title="never reviewed", assignee="dev-a")

    out, rc = run_land(good_id, bad_id, "--target", "origin/dev", "--json")
    by_id = {r["task_id"]: r for r in json.loads(out)}

    assert rc != 0, "a batch containing a refusal exits non-zero"
    assert by_id[good_id]["verdict"] == "landed"
    assert by_id[bad_id]["verdict"] == "refused"
    assert by_id[bad_id]["reason"] == "no_approval"
    # The healthy card really landed despite its sibling's refusal.
    git(repo.clone, "fetch", "origin", "dev")
    assert f"{good_id}.txt" in git(repo.clone, "ls-tree", "--name-only", "origin/dev")


def test_cli_uses_the_board_configured_target_and_names_it_in_output(kanban_home, repo):
    kb.write_board_metadata(None, land_target="origin/dev")
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
    out, rc = run_land(task_id, "--dry-run")
    assert rc == 0
    assert "origin/dev" in out, "output must always name the remote and branch"


def test_boards_set_land_target_persists_the_configuration(kanban_home):
    from hermes_cli import kanban as kc

    parser = argparse.ArgumentParser()
    kanban_parser = kc.build_parser(parser.add_subparsers(dest="_top"))
    args = kanban_parser.parse_args(
        ["boards", "set-land-target", "default", "origin/dev"]
    )
    assert kc.kanban_command(args) == 0
    assert kb.read_board_metadata("default")["land_target"] == "origin/dev"


def test_dry_run_does_not_even_fetch_the_remote(kanban_home, repo):
    """"Zero mutation" includes local refs: a dry run must not write
    remote-tracking refs either, so it can never move a reader's view."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        before = git(repo.clone, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes")
        kl.land_task(conn, task_id, target=("origin", "dev"), dry_run=True)
        after = git(repo.clone, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes")
    assert before == after


def test_landing_refuses_a_target_branch_the_remote_does_not_publish(kanban_home, repo):
    """Landing never creates a target branch it was not told exists."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "no-such-branch"))
    assert exc.value.reason == "target_unresolvable"
    assert repo.remote_sha("no-such-branch") == ""


def test_landing_leaves_no_staging_worktree_behind(kanban_home, repo):
    """Every merge happens in a throwaway tree; none may survive the run."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        kl.land_task(conn, task_id, target=("origin", "dev"))
    trees = git(repo.clone, "worktree", "list")
    assert "hermes-land-" not in trees


# ---------------------------------------------------------------------------
# Push endpoint — fetching and pushing must address ONE repository
# ---------------------------------------------------------------------------


def test_a_pushurl_pointing_elsewhere_is_the_endpoint_that_gets_verified(kanban_home, repo):
    """Git lets ``remote.origin.pushurl`` send writes to a different repository
    than ``ls-remote origin`` reads. Preflighting the fetch URL while pushing
    the push URL verifies a repo we never wrote. Everything must address the
    push endpoint, so the branch check itself fails on the real destination."""
    decoy = repo.root / "decoy.git"
    git(repo.root, "init", "--bare", "-b", "dev", str(decoy))
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        # The decoy has no branches at all, so it cannot carry the card's work.
        git(repo.clone, "config", "remote.origin.pushurl", str(decoy))

        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))

    assert exc.value.reason in {"branch_unpushed", "wrong_remote"}
    # Nothing was written to EITHER repository.
    assert git(repo.clone, "ls-remote", "--heads", str(decoy)) == ""


def test_landing_verifies_the_repository_it_actually_pushed_to(kanban_home, repo):
    """With a push URL that DOES carry the work, the read-back must follow the
    push — proving the receipt describes the repo that was written."""
    mirror = repo.root / "mirror.git"
    git(repo.root, "clone", "--bare", str(repo.remote), str(mirror))
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        git(path, "push", str(mirror), f"HEAD:refs/heads/wt/{task_id}")
        git(repo.clone, "config", "remote.origin.pushurl", str(mirror))
        before_fetch_url = repo.remote_sha("dev")

        result = kl.land_task(conn, task_id, target=("origin", "dev"))

    mirror_dev = git(repo.clone, "ls-remote", str(mirror), "refs/heads/dev").split("\t")[0]
    assert result["push_url"] == str(mirror)
    assert result["target_sha"] == mirror_dev, "the read-back must follow the push"
    assert repo.remote_sha("dev") == before_fetch_url, "the fetch URL was never written"


def test_a_remote_with_no_push_url_refuses_before_anything_happens(kanban_home, repo):
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        git(repo.clone, "config", "remote.origin.pushurl", "")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
    assert exc.value.reason == "remote_push_disabled"


def test_a_remote_with_several_push_urls_refuses_rather_than_pick_one(kanban_home, repo):
    """Two push URLs means one ``git push`` writes two repositories; a landing
    that reads back only one of them cannot honestly claim the content landed."""
    second = repo.root / "second.git"
    git(repo.root, "init", "--bare", "-b", "dev", str(second))
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        git(repo.clone, "config", "--add", "remote.origin.pushurl", str(repo.remote))
        git(repo.clone, "config", "--add", "remote.origin.pushurl", str(second))
        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
    assert exc.value.reason == "remote_push_ambiguous"


# ---------------------------------------------------------------------------
# Target races — the advance comes from a repository we do not have objects for
# ---------------------------------------------------------------------------


def test_landing_absorbs_a_target_advanced_by_a_foreign_clone(kanban_home, repo):
    """The realistic race: a teammate on another machine pushed to the target,
    so the new tip is not in our object database at all. Resolving the target
    with ``ls-remote`` alone yields a sha nothing local can check out."""
    other_clone = repo.root / "teammate"
    git(repo.root, "clone", str(repo.remote), str(other_clone))
    git(other_clone, "config", "user.name", "T")
    git(other_clone, "config", "user.email", "t@example.invalid")
    write(other_clone / "teammate.txt", "from another machine\n")
    git(other_clone, "add", "-A")
    git(other_clone, "commit", "-m", "foreign advance")
    git(other_clone, "push", "origin", "HEAD:refs/heads/dev")

    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        # Our clone has never seen that commit.
        foreign = repo.remote_sha("dev")
        assert git(repo.clone, "cat-file", "-t", foreign, check=False) == ""

        result = kl.land_task(conn, task_id, target=("origin", "dev"))

    git(repo.clone, "fetch", "origin", "dev")
    files = git(repo.clone, "ls-tree", "--name-only", "origin/dev").splitlines()
    assert result["verdict"] == "landed"
    assert f"{task_id}.txt" in files and "teammate.txt" in files


def test_a_target_that_advances_between_plan_and_push_reports_target_advanced(
    kanban_home, repo, monkeypatch,
):
    """The non-force push is what makes this safe; the reason code is what makes
    it actionable — 'someone moved it, re-run me', not 'branch protection'."""
    real_git = kl.git
    fired = {"done": False}

    def racing_git(cwd, *args, **kw):
        # Slip a competing commit onto the target in the instant before the push.
        if args and args[0] == "push" and not fired["done"]:
            fired["done"] = True
            racer = repo.root / "racer"
            git(repo.root, "clone", str(repo.remote), str(racer))
            git(racer, "config", "user.name", "T")
            git(racer, "config", "user.email", "t@example.invalid")
            write(racer / "racer.txt", "beat you to it\n")
            git(racer, "add", "-A")
            git(racer, "commit", "-m", "racing advance")
            git(racer, "push", "origin", "HEAD:refs/heads/dev")
        return real_git(cwd, *args, **kw)

    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        monkeypatch.setattr(kl, "git", racing_git)
        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
        monkeypatch.undo()
        status = kb.get_task(conn, task_id).status

    assert exc.value.reason == "target_advanced"
    assert status == "review", "a lost race leaves the card exactly as it was"
    git(repo.clone, "fetch", "origin", "dev")
    assert f"{task_id}.txt" not in git(
        repo.clone, "ls-tree", "--name-only", "origin/dev",
    ).splitlines()


# ---------------------------------------------------------------------------
# Idempotency — "already there" is a question about the CURRENT tip
# ---------------------------------------------------------------------------


def test_work_applied_and_then_reverted_is_not_treated_as_landed(kanban_home, repo):
    """The reason ``git cherry`` cannot answer this: an equivalent patch IS in
    the target's history, but the content is absent from the tip. Calling that
    'already landed' archives the card while the work is gone."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        other = repo.clone / ".worktrees" / "revert"
        git(repo.clone, "worktree", "add", "--detach", str(other), "origin/dev")
        git(other, "merge", "--squash", f"origin/wt/{task_id}")
        git(other, "commit", "-m", f"squashed {task_id}")
        git(other, "revert", "--no-edit", "HEAD")
        git(other, "push", "origin", "HEAD:refs/heads/dev")
        assert f"{task_id}.txt" not in git(
            other, "ls-tree", "--name-only", "HEAD",
        ).splitlines()

        result = kl.land_task(conn, task_id, target=("origin", "dev"))

    git(repo.clone, "fetch", "origin", "dev")
    assert result["verdict"] == "landed", "reverted work must be landed again, not skipped"
    assert f"{task_id}.txt" in git(
        repo.clone, "ls-tree", "--name-only", "origin/dev",
    ).splitlines()


def test_a_multi_commit_branch_squashed_onto_the_target_reads_as_landed(kanban_home, repo):
    """Per-commit patch ids do not survive a squash, so the commit-by-commit
    test misses exactly the case it exists for. The tree comparison does not."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="impl", assignee="dev-a")
        branch = f"wt/{task_id}"
        path = repo.clone / ".worktrees" / task_id
        git(repo.clone, "worktree", "add", "-b", branch, str(path), "origin/dev")
        for n in range(3):
            write(path / f"{task_id}-{n}.txt", f"part {n}\n")
            git(path, "add", "-A")
            git(path, "commit", "-m", f"part {n}")
        git(path, "push", "origin", f"HEAD:refs/heads/{branch}")
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, "
            "branch_name = ? WHERE id = ?", (str(path), branch, task_id),
        )
        conn.commit()
        head = git(path, "rev-parse", "HEAD")
        _hand_to_review(conn, task_id, summary="impl done")
        assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
        assert ka.approve_review_task(
            conn, task_id, source_sha=head, summary="approved",
            metadata={"pre_review_gate": {"pushed": head}},
        )[0]

        other = repo.clone / ".worktrees" / "squash3"
        git(repo.clone, "worktree", "add", "--detach", str(other), "origin/dev")
        git(other, "merge", "--squash", f"origin/{branch}")
        git(other, "commit", "-m", f"squashed {task_id}")
        git(other, "push", "origin", "HEAD:refs/heads/dev")
        after_squash = repo.remote_sha("dev")

        result = kl.land_task(conn, task_id, target=("origin", "dev"))

    assert result["verdict"] == "already_landed"
    assert result["readback"] == "patch_equivalent"
    assert repo.remote_sha("dev") == after_squash, "a squashed branch must not merge twice"


# ---------------------------------------------------------------------------
# Dry run — zero mutation includes the configured shell command
# ---------------------------------------------------------------------------


def test_dry_run_does_not_execute_the_configured_verification_command(kanban_home, repo, tmp_path):
    """``land_verify`` is arbitrary operator shell — running it during a dry run
    is precisely the external state a dry run promises not to touch. The plan is
    reported instead."""
    marker = tmp_path / "verify-ran"
    kb.write_board_metadata(None, land_verify=f"touch {marker}")
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        result = kl.land_task(conn, task_id, target=("origin", "dev"), dry_run=True)

    assert not marker.exists(), "a dry run must not run the configured command"
    assert result["verdict"] == "would_land"
    assert result["verification"] == {
        "kind": "command", "planned": True, "command": f"touch {marker}",
    }


def test_dry_run_creates_no_worktree_and_writes_no_ref(kanban_home, repo):
    """Staging worktrees are shared git metadata: a dry run that registers one
    mutates the repository every other worktree reads."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        before_trees = git(repo.clone, "worktree", "list")
        before_refs = git(repo.clone, "for-each-ref", "--format=%(refname) %(objectname)")

        kl.land_task(conn, task_id, target=("origin", "dev"), dry_run=True)

        assert git(repo.clone, "worktree", "list") == before_trees
        assert git(repo.clone, "for-each-ref", "--format=%(refname) %(objectname)") == before_refs


# ---------------------------------------------------------------------------
# Reporting — refusals name the resolved target; the receipt is one coherent record
# ---------------------------------------------------------------------------


def test_a_board_configured_target_is_named_in_refusal_output(kanban_home, repo):
    """A refusal that reports ``target: null`` while the board has one configured
    tells the operator nothing about what would have happened."""
    kb.write_board_metadata(None, land_target="origin/dev")
    with kbc.connect() as conn:
        unreviewed = kb.create_task(conn, title="never reviewed", assignee="dev-a")

    out, rc = run_land(unreviewed, "--json")
    record = json.loads(out)[0]

    assert rc != 0
    assert record["verdict"] == "refused" and record["reason"] == "no_approval"
    assert (record["remote"], record["branch"]) == ("origin", "dev")
    assert record["target"] == "origin/dev"

    human, _ = run_land(unreviewed)
    assert "origin/dev" in human, "human refusal output must name the target too"


def test_the_receipt_records_one_coherent_landing_identity(kanban_home, repo):
    """Everything a human or an auditor needs to reconstruct the landing, all
    describing the SAME commit: what was approved, what was pushed, and what the
    remote served back afterwards."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        approved_sha = repo.remote_sha(f"wt/{task_id}")
        result = kl.land_task(conn, task_id, target=("origin", "dev"))
        body = "\n".join(c.body for c in kb.list_comments(conn, task_id))

    assert result["approved_sha"] == approved_sha == result["source_sha"]
    assert result["readback_sha"] == result["target_sha"] == repo.remote_sha("dev")
    assert result["reviewer"] == "reviewer"
    assert result["landed_at"] > 0
    assert "read-back" in result["closure_reason"]
    for expected in (
        approved_sha, result["target_sha"], result["target_sha_before"],
        "origin", "dev", "reviewer", str(result["approval_run_id"]),
    ):
        assert expected in body, f"receipt must record {expected!r}"


def test_a_target_moving_during_readback_refuses_rather_than_record_the_wrong_sha(
    kanban_home, repo, monkeypatch,
):
    """The read-back is two steps — ask the remote what it publishes, then fetch
    that to prove the content is reachable. If the target moves between them,
    the sha we would RECORD and the object we actually PROVED are different
    commits, and the receipt would be a claim about something never verified.
    Refusing keeps the record honest; re-running then reports ``already_landed``.
    """
    real_git = kl.git
    fired = {"done": False}

    def racing_git(cwd, *args, **kw):
        # Move the target in the window between the read-back ls-remote and the
        # read-back fetch — after our own push has already succeeded.
        if (
            args and args[0] == "fetch" and not fired["done"]
            and any("refs/hermes-land/readback" in str(a) for a in args)
        ):
            fired["done"] = True
            racer = repo.root / "readback-racer"
            git(repo.root, "clone", str(repo.remote), str(racer))
            git(racer, "config", "user.name", "T")
            git(racer, "config", "user.email", "t@example.invalid")
            write(racer / "later.txt", "landed after you\n")
            git(racer, "add", "-A")
            git(racer, "commit", "-m", "post-push advance")
            git(racer, "push", "origin", "HEAD:refs/heads/dev")
        return real_git(cwd, *args, **kw)

    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        monkeypatch.setattr(kl, "git", racing_git)
        with pytest.raises(kl.LandRefusal) as exc:
            kl.land_task(conn, task_id, target=("origin", "dev"))
        monkeypatch.undo()
        assert fired["done"], "the race must actually have been injected"
        status = kb.get_task(conn, task_id).status

        # The content really did land; the refusal is about the PROOF, not the
        # merge, so an immediate re-run finishes the bookkeeping cleanly.
        assert status == "review", "an unproven landing must not close the card"
        again = kl.land_task(conn, task_id, target=("origin", "dev"))

    assert exc.value.reason == "readback_failed"
    assert again["verdict"] == "already_landed"
    assert again["readback_sha"] == again["target_sha"] == repo.remote_sha("dev")


def test_verification_accepts_the_implementers_pre_review_gate_receipt(kanban_home, repo):
    """The verification receipt is produced by the IMPLEMENTER and recorded on
    the review handoff — that is where `pre_review_gate` actually lives in
    practice. A reviewer approving the card does not retype it, so looking only
    at the approval run makes every real card refuse `verification_missing`.

    Safety is unchanged: the receipt is accepted only when it names the exact
    commit being landed, and that commit is already pinned to the approval.
    """
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="impl", assignee="dev-a")
        path = repo.task_worktree(task_id)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, "
            "branch_name = ? WHERE id = ?", (str(path), f"wt/{task_id}", task_id),
        )
        conn.commit()
        head = git(path, "rev-parse", "HEAD")

        # Implementer hands off WITH the gate receipt, the way a worker does.
        task = kb.claim_task(conn, task_id, claimer=f"lock-impl-{task_id}")
        assert kb.request_review(
            conn, task_id, summary="impl done", reviewer="reviewer",
            expected_run_id=task.current_run_id,
            metadata={"pre_review_gate": {"clean": True, "pushed": head}},
        )
        # Reviewer approves WITHOUT restating any of it.
        assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
        assert ka.approve_review_task(conn, task_id, source_sha=head)[0]

        source = kl.source_state(conn, task_id, remote="origin")
        receipt = kl.verify(conn, task_id, source, board=None)

    assert receipt["kind"] == "receipt"
    assert receipt["sha"] == head


def test_an_implementer_receipt_for_a_different_commit_is_still_refused(kanban_home, repo):
    """Widening where the receipt may live must not widen WHAT it proves."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="impl", assignee="dev-a")
        path = repo.task_worktree(task_id)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, "
            "branch_name = ? WHERE id = ?", (str(path), f"wt/{task_id}", task_id),
        )
        conn.commit()
        head = git(path, "rev-parse", "HEAD")

        task = kb.claim_task(conn, task_id, claimer=f"lock-impl-{task_id}")
        assert kb.request_review(
            conn, task_id, summary="impl done", reviewer="reviewer",
            expected_run_id=task.current_run_id,
            metadata={"pre_review_gate": {"pushed": "0" * 40}},
        )
        assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
        assert ka.approve_review_task(conn, task_id, source_sha=head)[0]

        source = kl.source_state(conn, task_id, remote="origin")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.verify(conn, task_id, source, board=None)
    assert exc.value.reason == "verification_stale"
