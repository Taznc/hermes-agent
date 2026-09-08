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

    def task_worktree(self, task_id: str, *, content: str = "feature\n",
                      push: bool = True, dirty: bool = False) -> Path:
        branch = f"wt/{task_id}"
        path = self.clone / ".worktrees" / task_id
        git(self.clone, "worktree", "add", "-b", branch, str(path), "origin/dev")
        write(path / f"{task_id}.txt", content)
        git(path, "add", "-A")
        git(path, "commit", "-m", f"work for {task_id}")
        if push:
            git(path, "push", "origin", f"HEAD:refs/heads/{branch}")
            git(path, "branch", f"--set-upstream-to=origin/{branch}", branch)
        if dirty:
            write(path / "scratch.txt", "uncommitted\n")
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
    """An approved card whose workspace is a real pushed task worktree."""
    task_id = kb.create_task(conn, title="impl", assignee="dev-a")
    path = repo.task_worktree(task_id, **worktree_kw)
    conn.execute(
        "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, branch_name = ? "
        "WHERE id = ?", (str(path), f"wt/{task_id}", task_id),
    )
    conn.commit()
    _hand_to_review(conn, task_id, summary="impl done")
    assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
    assert kb.complete_task(
        conn, task_id, summary="approved",
        metadata={"pre_review_gate": {"pushed": git(path, "rev-parse", "HEAD")}},
    )
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


def _review_cycle(conn, *, approve: bool, request_changes_after: bool = False) -> str:
    """Drive a task through the REAL review lifecycle and return its id."""
    task_id = kb.create_task(conn, title="impl", assignee="dev-a")
    _hand_to_review(conn, task_id, summary="done")
    assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
    if approve:
        assert kb.complete_task(conn, task_id, summary="approved by reviewer")
    if request_changes_after:
        # A reopened card genuinely goes back through review: archive/unarchive
        # is the supported path out of ``done``, and clears completion evidence.
        assert kb.archive_task(conn, task_id)
        assert kb.unarchive_task(conn, task_id, status="ready")
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


def test_approval_verdict_reads_the_reviewer_run_that_completed_the_card(kanban_home):
    with kbc.connect() as conn:
        task_id = _review_cycle(conn, approve=True)
        verdict = kl.approval_verdict(conn, task_id)
    assert verdict.run_id is not None
    assert verdict.reviewer == "reviewer"
    assert verdict.summary == "approved by reviewer"


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
    """Nothing to land: the commit exists only in the local worktree."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo, push=False)
        with pytest.raises(kl.LandRefusal) as exc:
            kl.source_state(conn, task_id, remote="origin")
    assert exc.value.reason == "branch_unpushed"


def test_source_state_refuses_a_dirty_surviving_worktree(kanban_home, repo):
    """Uncommitted work in the task worktree means the pushed sha is not the
    whole change — refuse rather than land a partial diff."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo, dirty=True)
        assert path.is_dir(), "dirty worktree must survive completion cleanup"
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


def test_verification_refuses_when_no_receipt_and_no_command_exist(kanban_home, repo):
    """An approval with no verification evidence at all must not land."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        conn.execute(
            "UPDATE task_runs SET metadata = NULL WHERE task_id = ? AND outcome = 'completed'",
            (task_id,),
        )
        conn.commit()
        source = kl.source_state(conn, task_id, remote="origin")
        with pytest.raises(kl.LandRefusal) as exc:
            kl.verify(conn, task_id, source, board=None)
    assert exc.value.reason == "verification_missing"


def test_verification_refuses_a_receipt_naming_a_different_sha(kanban_home, repo):
    """A receipt from an earlier commit does not vouch for what would land."""
    with kbc.connect() as conn:
        task_id, path = make_approved_task(conn, repo)
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE task_id = ? AND outcome = 'completed'",
            (json.dumps({"pre_review_gate": {"pushed": "0" * 40}}), task_id),
        )
        conn.commit()
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
    assert status_after == "done", "dry run must not close the card"


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
    assert status == "done", "a rejected push must leave the card open"


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
