"""hermes update must refuse a checkout that deploys from a non-origin remote."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_fork import update_guard


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.delenv(update_guard.BYPASS_ENV, raising=False)
    upstream = tmp_path / "upstream"
    fork = tmp_path / "fork"
    for bare in (upstream, fork):
        _git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = tmp_path / "runtime"
    _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "push", "-q", "origin", "dev")
    _git(repo, "push", "-q", "fork", "dev")
    return repo


def test_fork_tracking_checkout_is_refused(checkout):
    _git(checkout, "branch", "-q", "--set-upstream-to=fork/dev")
    message = update_guard.cross_remote_refusal(checkout)
    assert message is not None
    assert "fork/dev" in message and "origin" in message
    assert "merge --ff-only fork/dev" in message


def test_origin_tracking_checkout_is_allowed(checkout):
    _git(checkout, "branch", "-q", "--set-upstream-to=origin/dev")
    assert update_guard.cross_remote_refusal(checkout) is None


def test_untracked_or_detached_checkout_is_allowed(checkout):
    assert update_guard.cross_remote_refusal(checkout) is None
    _git(checkout, "checkout", "-q", "--detach")
    assert update_guard.cross_remote_refusal(checkout) is None


def test_bypass_env_allows_one_run(checkout, monkeypatch):
    _git(checkout, "branch", "-q", "--set-upstream-to=fork/dev")
    monkeypatch.setenv(update_guard.BYPASS_ENV, "1")
    assert update_guard.cross_remote_refusal(checkout) is None


def test_update_preflight_exits_before_any_mutation(checkout, monkeypatch):
    """The CLI entry point refuses (exit 2) before fetch/merge — --check/--plan still work."""
    import argparse

    from hermes_cli import main as hermes_main
    from hermes_cli import update_contract

    _git(checkout, "branch", "-q", "--set-upstream-to=fork/dev")
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda root: None)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True,
    ).stdout
    with pytest.raises(SystemExit) as exc:
        hermes_main._update_preflight_handled(argparse.Namespace(plan=False, check=False, branch=None))
    assert exc.value.code == 2
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True,
    ).stdout
    assert head_before == head_after
