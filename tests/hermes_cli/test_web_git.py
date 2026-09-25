"""Tests for ``hermes_cli.web_git``'s coding-rail status fields.

Covers ``repo_status``'s ``unpushed`` and ``mergedIntoBase`` fields, mirroring
the Electron ``repoStatus`` contract exercised in
``apps/desktop/electron/git-review-ops.test.ts``.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from hermes_cli import web_git

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "HOME": "", "PATH": os.environ.get("PATH", ""),
}


def _git(repo, *args):
    env = {**_ENV, "HOME": str(repo)}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


@requires_git
def test_repo_status_unpushed_and_unmerged_on_a_local_only_branch(repo):
    _git(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("hi\nbye\n")
    _git(repo, "commit", "-qam", "work")

    status = web_git.repo_status(str(repo))

    assert status is not None
    assert status["unpushed"] == 1
    assert status["mergedIntoBase"] is False


@requires_git
def test_repo_status_clean_and_merged_on_the_default_branch_itself(repo):
    status = web_git.repo_status(str(repo))

    assert status is not None
    assert status["unpushed"] == 0
    assert status["mergedIntoBase"] is True


@requires_git
def test_repo_status_merged_is_none_on_a_detached_head(repo):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(repo, "checkout", "-q", head)

    status = web_git.repo_status(str(repo))

    assert status is not None
    assert status["mergedIntoBase"] is None
