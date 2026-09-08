"""Kanban worker preservation safety net.

Recoverable work in a task's own git worktree (dirty tree, or local commits
never pushed) must not remain only on the VM when a run ends or is reclaimed.
:mod:`hermes_cli.kanban_preserve` commits and pushes it — and nothing else:
never merges, rebases, force-pushes, switches branches, or deletes anything.

Every ambiguity (ownership, branch, content safety, remote state) fails
CLOSED: no commit, an actionable event, and the worktree preserved so
``_cleanup_worktree_workspace`` still refuses to remove it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_preserve as kp


def _git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if check:
        assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures — a real repo, a real local bare remote, a real linked worktree.
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project clone of a local bare remote, one commit deep on ``main``."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "--initial-branch=main", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("config", "user.email", "t@example.com", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    (project / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    _git("add", "-A", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    _git("push", "-u", "origin", "main", cwd=project)
    return project


@pytest.fixture
def worktree(repo: Path) -> Path:
    """A linked task worktree on its own ``wt/t_demo`` branch."""
    wt = repo / ".worktrees" / "t_demo"
    _git("worktree", "add", "-b", "wt/t_demo", str(wt), "main", cwd=repo)
    return wt


def _head(path: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=path).strip()


def _remote_branch_head(repo: Path, branch: str) -> str | None:
    out = _git(
        "ls-remote", "origin", f"refs/heads/{branch}", cwd=repo, check=False
    ).strip()
    return out.split("\t")[0] if out else None


# ---------------------------------------------------------------------------
# Tracked edits
# ---------------------------------------------------------------------------


def test_tracked_edit_is_committed_on_the_task_branch_and_pushed(
    repo: Path, worktree: Path
) -> None:
    (worktree / "README.md").write_text("hello\nworker edit\n", encoding="utf-8")
    before = _head(worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    assert result.commit_sha and result.commit_sha != before
    assert result.pushed is True
    assert result.push_error is None
    # Committed on the SAME branch — never switched, never detached.
    assert _git("branch", "--show-current", cwd=worktree).strip() == "wt/t_demo"
    assert _head(worktree) == result.commit_sha
    # And the commit is genuinely on the remote now.
    assert _remote_branch_head(repo, "wt/t_demo") == result.commit_sha
    # Tree is clean afterwards: the edit was captured, not left behind.
    assert _git("status", "--porcelain", cwd=worktree).strip() == ""


# ---------------------------------------------------------------------------
# Nothing to preserve
# ---------------------------------------------------------------------------


def test_clean_and_fully_pushed_worktree_is_a_no_op(repo: Path, worktree: Path) -> None:
    _git("push", "-u", "origin", "wt/t_demo", cwd=worktree)
    before = _head(worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "nothing_to_preserve", result
    assert result.commit_sha is None
    # No commit was created and nothing was pushed.
    assert _head(worktree) == before


def test_existing_unpushed_commit_is_pushed_without_a_new_commit(
    repo: Path, worktree: Path
) -> None:
    (worktree / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-m", "worker commit", cwd=worktree)
    existing = _head(worktree)
    assert _remote_branch_head(repo, "wt/t_demo") is None

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    assert result.pushed is True
    # The worker's own commit is preserved as-is: no safety commit on top.
    assert _head(worktree) == existing
    assert result.commit_sha == existing
    assert _remote_branch_head(repo, "wt/t_demo") == existing


# ---------------------------------------------------------------------------
# Content safety
# ---------------------------------------------------------------------------


def test_gitignored_and_build_artifacts_are_excluded_from_the_snapshot(
    repo: Path, worktree: Path
) -> None:
    (worktree / "src.py").write_text("x = 1\n", encoding="utf-8")
    (worktree / "debug.log").write_text("noise\n", encoding="utf-8")  # gitignored
    (worktree / "build").mkdir()
    (worktree / "build" / "out.bin").write_text("blob\n", encoding="utf-8")  # gitignored

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    files = _git("show", "--name-only", "--format=", "HEAD", cwd=worktree).split()
    assert files == ["src.py"]


def test_untracked_generated_directory_is_refused_not_committed(
    repo: Path, worktree: Path
) -> None:
    """``node_modules/`` is not gitignored in this repo, so only the artifact
    guard stands between it and a multi-thousand-file safety commit."""
    (worktree / "node_modules" / "pkg").mkdir(parents=True)
    (worktree / "node_modules" / "pkg" / "index.js").write_text("//\n", encoding="utf-8")
    before = _head(worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "generated_artifact"
    assert "node_modules" in (result.detail or "")
    # Fail closed: no commit, nothing pushed, work still on disk for a human.
    assert _head(worktree) == before
    assert (worktree / "node_modules" / "pkg" / "index.js").exists()


def test_suspected_secret_fails_closed_without_committing(
    repo: Path, worktree: Path
) -> None:
    (worktree / "safe.py").write_text("x = 1\n", encoding="utf-8")
    # A real-shaped credential the redactor recognizes by prefix.
    (worktree / "notes.md").write_text(
        "deploy key: ghp_" + "A" * 36 + "\n", encoding="utf-8"
    )
    before = _head(worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "suspected_secret"
    assert "notes.md" in (result.detail or "")
    # The secret is NOT echoed into the actionable record.
    assert "ghp_" not in (result.detail or "")
    assert _head(worktree) == before
    # And the safe file was not committed either — the snapshot is all-or-nothing.
    assert _git("status", "--porcelain", cwd=worktree).strip() != ""


def test_secret_named_file_is_refused_even_when_its_contents_look_benign(
    repo: Path, worktree: Path
) -> None:
    (worktree / ".env").write_text("PORT=8080\n", encoding="utf-8")

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "suspected_secret"
    assert ".env" in (result.detail or "")


def test_oversized_file_is_refused(repo: Path, worktree: Path) -> None:
    (worktree / "huge.dat").write_bytes(b"\0" * (kp.DEFAULT_MAX_FILE_BYTES + 1))
    before = _head(worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "oversized"
    assert "huge.dat" in (result.detail or "")
    assert _head(worktree) == before


def test_a_staged_rename_is_preserved_and_its_source_record_is_not_a_candidate(
    repo: Path, worktree: Path
) -> None:
    """``status --porcelain -z`` emits a rename as TWO records; the second is
    the ORIGINAL path and must be consumed as data, not scanned as a file."""
    _git("mv", "README.md", "DOCS.md", cwd=worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    files = _git("show", "--name-status", "--format=", "HEAD", cwd=worktree)
    assert "DOCS.md" in files
    assert _git("status", "--porcelain", cwd=worktree).strip() == ""


def test_a_file_renamed_to_a_secret_name_is_still_refused(
    repo: Path, worktree: Path
) -> None:
    _git("mv", "README.md", "id_rsa", cwd=worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "suspected_secret"
    assert "id_rsa" in (result.detail or "")


def test_a_path_with_leading_whitespace_is_read_verbatim(
    repo: Path, worktree: Path
) -> None:
    """``-z`` status output is NOT quoted, so the path runs verbatim from a
    fixed offset to the NUL. Trimming it would make the guard scan the wrong
    file — here, missing that this candidate is a credential-named one."""
    (worktree / " id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "suspected_secret"
    assert "id_rsa" in (result.detail or "")


# ---------------------------------------------------------------------------
# Remote state
# ---------------------------------------------------------------------------


def test_push_rejection_keeps_the_commit_and_reports_the_failure(
    repo: Path, worktree: Path
) -> None:
    """A non-fast-forward rejection must never discard local work, and must
    never escalate to a force push."""
    # Publish a diverging remote branch the worktree cannot fast-forward.
    other = repo / "other"
    _git("clone", str(repo.parent / "origin.git"), str(other))
    _git("config", "user.email", "t@example.com", cwd=other)
    _git("config", "user.name", "Test", cwd=other)
    _git("checkout", "-b", "wt/t_demo", cwd=other)
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _git("add", "-A", cwd=other)
    _git("commit", "-m", "remote side", cwd=other)
    _git("push", "origin", "wt/t_demo", cwd=other)
    remote_before = _remote_branch_head(repo, "wt/t_demo")

    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")
    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    assert result.pushed is False
    assert result.push_error
    # The work is committed locally — never dropped because the push failed.
    assert result.commit_sha == _head(worktree)
    assert _git("status", "--porcelain", cwd=worktree).strip() == ""
    # And the remote was NOT force-overwritten.
    assert _remote_branch_head(repo, "wt/t_demo") == remote_before


def test_offline_remote_still_commits_and_reports_push_failure(
    repo: Path, worktree: Path, tmp_path: Path
) -> None:
    _git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=repo)
    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    assert result.pushed is False
    assert result.push_error
    assert result.commit_sha == _head(worktree)


def test_existing_unpushed_commit_is_pushed_when_remote_tracking_refs_are_empty(
    repo: Path, worktree: Path
) -> None:
    """A remote can be CONFIGURED with no cached tracking refs (never fetched,
    or the branch was pushed with ``--no-track``). That is ambiguous, not
    proof nothing is unpushed — treating it as "nothing to push" silently
    drops a real local commit."""
    (worktree / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-m", "worker commit", cwd=worktree)
    existing = _head(worktree)
    # Simulate "no cached tracking refs" without touching the remote itself:
    # delete the one local remote-tracking ref for this repo's own origin.
    _git("update-ref", "-d", "refs/remotes/origin/main", cwd=worktree, check=False)
    assert _git(
        "for-each-ref", "--format=%(refname)", "refs/remotes", cwd=worktree
    ).strip() == "", "precondition: no cached remote-tracking refs"
    assert _remote_branch_head(repo, "wt/t_demo") is None

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    assert result.pushed is True
    assert result.commit_sha == existing
    assert _remote_branch_head(repo, "wt/t_demo") == existing


def test_repo_without_a_remote_commits_and_reports_no_remote(
    tmp_path: Path
) -> None:
    solo = tmp_path / "solo"
    _git("init", "--initial-branch=main", str(solo))
    _git("config", "user.email", "t@example.com", cwd=solo)
    _git("config", "user.name", "Test", cwd=solo)
    (solo / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", "-A", cwd=solo)
    _git("commit", "-m", "init", cwd=solo)
    wt = solo / ".worktrees" / "t_solo"
    _git("worktree", "add", "-b", "wt/t_solo", str(wt), "main", cwd=solo)
    (wt / "b.txt").write_text("b\n", encoding="utf-8")

    result = kp.preserve_worktree(wt, "wt/t_solo")

    assert result.status == "preserved", result
    assert result.pushed is False
    assert result.push_error == "no_remote_configured"
    assert result.commit_sha == _head(wt)


# ---------------------------------------------------------------------------
# Nested gitignore
# ---------------------------------------------------------------------------


def test_nested_gitignored_files_inside_an_expanded_directory_are_excluded(
    repo: Path, worktree: Path
) -> None:
    """An untracked directory collapses to one ``dir/`` candidate in
    ``git status``; expanding it must still respect a ``.gitignore`` nested
    INSIDE that directory, not just top-level ignore rules."""
    pkg = worktree / "pkg"
    pkg.mkdir()
    (pkg / ".gitignore").write_text(".env\n", encoding="utf-8")
    (pkg / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (pkg / "safe.py").write_text("x = 1\n", encoding="utf-8")

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    files = _git("show", "--name-only", "--format=", "HEAD", cwd=worktree).split()
    assert "pkg/safe.py" in files
    assert "pkg/.env" not in files


# ---------------------------------------------------------------------------
# Credential-scan availability
# ---------------------------------------------------------------------------


def test_credential_scan_being_unavailable_fails_closed(
    repo: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the redactor dependency cannot be imported, the scan result is
    ambiguous, not \"clean\" — a real secret must not slip through because the
    safety dependency happened to be unavailable."""
    (worktree / "notes.md").write_text(
        "deploy key: ghp_" + "A" * 36 + "\n", encoding="utf-8"
    )
    before = _head(worktree)
    monkeypatch.setattr(kp, "_contains_credential", lambda path: None)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "unsafe", result
    assert result.reason == "credential_scan_unavailable"
    assert _head(worktree) == before
    assert _git("status", "--porcelain", cwd=worktree).strip() != ""


# ---------------------------------------------------------------------------
# Push exceptions must not lose the local commit SHA
# ---------------------------------------------------------------------------


def test_a_git_timeout_during_push_still_reports_the_local_commit(
    repo: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push that times out must never look like the commit never happened —
    the local SHA is real and must survive into the result."""
    import subprocess as _subprocess

    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")
    real_run = _subprocess.run

    def _flaky_run(args, **kwargs):
        if "push" in args:
            raise _subprocess.TimeoutExpired(cmd=args, timeout=60)
        return real_run(args, **kwargs)

    monkeypatch.setattr(kp.subprocess, "run", _flaky_run)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "preserved", result
    assert result.commit_sha == _head(worktree)
    assert result.pushed is False
    assert result.push_error
    assert "timed out" in result.push_error


# ---------------------------------------------------------------------------
# Lock: fail closed when exclusion cannot be established, self-heal on stale
# ---------------------------------------------------------------------------


def test_lock_fails_closed_when_the_git_dir_is_unresolvable(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If exclusion cannot even be established, proceeding unlocked IS the
    race this guards against — it must refuse, not silently "always acquire"."""
    monkeypatch.setattr(kp, "_git_out", lambda *a, **k: None)

    with kp._preserve_lock(worktree, "t_demo") as acquired:
        assert acquired is False


def test_lock_fails_closed_when_the_platform_lock_cannot_be_taken(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kp, "_try_lock_fd", lambda fd: False)

    with kp._preserve_lock(worktree, "t_demo") as acquired:
        assert acquired is False


@pytest.mark.windows_only
def test_windows_lock_backend_excludes_a_second_descriptor(tmp_path: Path) -> None:
    """Run the real msvcrt locking path on the native Windows CI lane."""
    import os

    path = tmp_path / "preservation.lock"
    first = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert kp._try_lock_fd(first) is True
        assert kp._try_lock_fd(second) is False
    finally:
        kp._unlock_fd(first)
        os.close(first)
        os.close(second)


def test_a_lock_file_left_by_a_dead_process_is_reused(
    worktree: Path,
) -> None:
    """Kernel locks auto-release on process death, so stale file contents must
    not permanently block preservation and never require pathname replacement."""
    git_dir = kp._git_out(worktree, "rev-parse", "--path-format=absolute", "--git-dir")
    lock_path = Path(git_dir) / "hermes-kanban-preserve-t_demo.lock"
    # A pid essentially guaranteed to be dead.
    lock_path.write_text("999999999", encoding="utf-8")

    with kp._preserve_lock(worktree, "t_demo") as acquired:
        assert acquired is True


def test_stale_lock_recovery_never_unlinks_the_shared_lock_path(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale recovery must never delete by pathname after observing old
    contents: another caller may have replaced that path with its live claim in
    between. OS locks auto-release on process death, so a leftover file can be
    reused in place without any compare/delete race."""
    git_dir = kp._git_out(worktree, "rev-parse", "--path-format=absolute", "--git-dir")
    lock_path = Path(git_dir) / "hermes-kanban-preserve-t_demo.lock"
    lock_path.write_text("999999999", encoding="utf-8")
    unlinked: list[Path] = []
    real_unlink = Path.unlink

    def _track_unlink(path: Path, *args, **kwargs):
        unlinked.append(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _track_unlink)

    with kp._preserve_lock(worktree, "t_demo") as acquired:
        assert acquired is True

    assert unlinked == []





def test_detached_head_is_refused(repo: Path, worktree: Path) -> None:
    _git("checkout", "--detach", cwd=worktree)
    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")
    before = _head(worktree)

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "skipped", result
    assert result.reason == "detached_head"
    assert _head(worktree) == before
    assert _git("status", "--porcelain", cwd=worktree).strip() != ""


def test_wrong_branch_is_refused_and_never_switched(
    repo: Path, worktree: Path
) -> None:
    """The worktree sitting on some other branch means our ownership belief is
    wrong; committing there would put this task's work on a stranger's branch."""
    _git("checkout", "-b", "someone-else", cwd=worktree)
    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")

    result = kp.preserve_worktree(worktree, "wt/t_demo")

    assert result.status == "skipped", result
    assert result.reason == "branch_mismatch"
    # Never switched branches to "fix" it.
    assert _git("branch", "--show-current", cwd=worktree).strip() == "someone-else"
    assert _git("status", "--porcelain", cwd=worktree).strip() != ""


def test_non_git_path_is_a_safe_skip(tmp_path: Path) -> None:
    plain = tmp_path / "scratchdir"
    plain.mkdir()
    (plain / "note.txt").write_text("hi\n", encoding="utf-8")

    result = kp.preserve_worktree(plain, "wt/t_demo")

    assert result.status == "skipped", result
    assert result.reason == "not_a_git_worktree"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_preservation_creates_exactly_one_safety_commit(
    repo: Path, worktree: Path
) -> None:
    """Completion and reclaim can fire on the same worktree at once. The loser
    must not stack a second snapshot commit on the winner's."""
    import threading

    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")
    before = _head(worktree)
    results: list[kp.PreserveResult] = []
    start = threading.Barrier(2)

    def run() -> None:
        start.wait(timeout=10)
        results.append(kp.preserve_worktree(worktree, "wt/t_demo"))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 2
    statuses = sorted(r.status for r in results)
    # One did the work; the other found nothing left or stood down on the lock.
    assert statuses[0] in {"nothing_to_preserve", "preserved", "skipped"}
    assert "preserved" in statuses
    # Exactly ONE commit was added on top of the pre-race HEAD.
    added = _git("rev-list", "--count", f"{before}..HEAD", cwd=worktree).strip()
    assert added == "1", _git("log", "--oneline", f"{before}..HEAD", cwd=worktree)
    for r in results:
        if r.status == "skipped":
            assert r.reason == "concurrent"


def test_lock_holder_makes_a_second_caller_stand_down(
    repo: Path, worktree: Path
) -> None:
    (worktree / "mine.txt").write_text("mine\n", encoding="utf-8")
    before = _head(worktree)

    with kp._preserve_lock(worktree, "t_demo") as held:
        assert held is True
        result = kp.preserve_worktree(worktree, "wt/t_demo", task_id="t_demo")

    assert result.status == "skipped", result
    assert result.reason == "concurrent"
    # Fail closed: no commit while another preserver owns the worktree.
    assert _head(worktree) == before
