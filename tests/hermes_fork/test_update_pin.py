"""hermes_fork.update_pin: fork update pin (branch + origin) for ``hermes update``."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_fork import update_pin as up

FORK = "https://github.com/Taznc/hermes-agent.git"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "remote", "add", "origin", FORK)
    return root


def _pin(root: Path, branch: str = "dev", origin: str | None = FORK) -> None:
    _git(root, "config", up.BRANCH_KEY, branch)
    if origin:
        _git(root, "config", up.ORIGIN_KEY, origin)


def _args(**kw) -> argparse.Namespace:
    base = {"branch": None, "channel": None, "set_channel": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_unpinned_install_is_untouched(repo: Path) -> None:
    args = _args()
    assert up.enforce_update_pin(args, repo) is None
    assert args.branch is None
    assert up.pinned_check_target(repo, None, None) == (None, None)


def test_pin_sets_branch_when_none_given(repo: Path) -> None:
    _pin(repo)
    args = _args()
    pin = up.enforce_update_pin(args, repo)
    assert pin == up.UpdatePin("dev", FORK)
    assert args.branch == "dev"


def test_matching_explicit_branch_is_allowed(repo: Path) -> None:
    _pin(repo)
    args = _args(branch="dev")
    up.enforce_update_pin(args, repo)
    assert args.branch == "dev"


@pytest.mark.parametrize("kw", [{"branch": "main"}, {"channel": "stable"}, {"set_channel": "canary"}])
def test_conflicting_request_is_refused(repo: Path, kw: dict) -> None:
    _pin(repo)
    with pytest.raises(SystemExit) as exc:
        up.enforce_update_pin(_args(**kw), repo)
    assert exc.value.code == 1


def test_wrong_origin_is_refused(repo: Path) -> None:
    _pin(repo)
    _git(repo, "remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent.git")
    with pytest.raises(SystemExit):
        up.enforce_update_pin(_args(), repo)


def test_ssh_form_of_pinned_origin_is_accepted(repo: Path) -> None:
    _pin(repo)
    _git(repo, "remote", "set-url", "origin", "git@github.com:Taznc/hermes-agent")
    assert up.enforce_update_pin(_args(), repo) is not None


def test_insteadof_redirect_is_refused(repo: Path) -> None:
    """A repo-level url.insteadOf that reroutes origin's fetch is caught."""
    _pin(repo)
    _git(repo, "config", "url.https://github.com/NousResearch/.insteadOf", "https://github.com/Taznc/")
    with pytest.raises(SystemExit):
        up.enforce_update_pin(_args(), repo)


def test_env_cannot_create_or_change_the_pin(repo: Path, monkeypatch) -> None:
    """Git env config injection neither creates a pin nor changes an existing one."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", up.BRANCH_KEY)
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "main")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'{up.BRANCH_KEY}'='main'")
    assert up.read_pin(repo) is None
    _pin(repo)
    assert up.read_pin(repo) == up.UpdatePin("dev", FORK)


def test_env_cannot_redirect_the_repository(repo: Path, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", up.BRANCH_KEY, "main")
    _pin(repo)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert up.read_pin(repo) == up.UpdatePin("dev", FORK)


def test_enforce_strips_git_overrides_from_process_env(repo: Path, monkeypatch) -> None:
    import os

    _pin(repo)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://example.invalid/x.git")
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    up.enforce_update_pin(_args(), repo)
    assert not any(k in os.environ for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_DIR"))


def test_invalid_pinned_branch_is_refused(repo: Path) -> None:
    _git(repo, "config", up.BRANCH_KEY, "--upload-pack=evil")
    with pytest.raises(SystemExit):
        up.enforce_update_pin(_args(), repo)


def test_pin_without_origin_only_pins_branch(repo: Path) -> None:
    _pin(repo, origin=None)
    _git(repo, "remote", "set-url", "origin", "https://example.com/anything.git")
    args = _args()
    assert up.enforce_update_pin(args, repo) == up.UpdatePin("dev", None)
    assert args.branch == "dev"


def test_check_target_follows_the_pin(repo: Path) -> None:
    _pin(repo)
    assert up.pinned_check_target(repo, None, None) == ("dev", "main")
    assert up.pinned_check_target(repo, "main", "stable") == ("dev", "main")


def test_pin_is_shared_by_linked_worktrees(repo: Path, tmp_path: Path) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _pin(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    assert up.read_pin(wt) == up.UpdatePin("dev", FORK)


@pytest.mark.parametrize(("url", "key"), [
    ("https://github.com/Taznc/hermes-agent.git", "github.com/taznc/hermes-agent"),
    ("https://user:tok@github.com/Taznc/hermes-agent", "github.com/taznc/hermes-agent"),
    ("git@github.com:Taznc/hermes-agent.git", "github.com/taznc/hermes-agent"),
    ("ssh://git@github.com:22/Taznc/hermes-agent.git", "github.com/taznc/hermes-agent"),
    ("https://github.com/NousResearch/hermes-agent.git", "github.com/nousresearch/hermes-agent"),
])
def test_normalize_repo_url(url: str, key: str) -> None:
    assert up.normalize_repo_url(url) == key


@pytest.fixture()
def remote_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' with main + dev (dev one commit ahead) and a clone parked on main."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(seed, "config", k, v)
    _git(seed, "commit", "-q", "--allow-empty", "-m", "base")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "fork work")
    _git(seed, "push", "-q", "origin", "HEAD:dev")
    clone = tmp_path / "install"
    _git(tmp_path, "clone", "-q", "-b", "main", str(bare), str(clone))
    _git(clone, "fetch", "-q", "origin", "dev:refs/remotes/origin/dev")
    return bare, clone


def test_update_check_reports_against_the_pin(remote_pair, tmp_path: Path) -> None:
    from hermes_cli.source_check import check_for_updates

    _, clone = remote_pair
    home = tmp_path / "home"
    home.mkdir()
    unpinned = check_for_updates(install_root=clone, home=home, force=True,
                                 cache_path=tmp_path / "c1.json", branch_config_path=None)
    assert unpinned.get("branch") == "main"

    _pin(clone, origin=None)
    pinned = check_for_updates(install_root=clone, home=home, force=True,
                               cache_path=tmp_path / "c2.json", branch_config_path=None)
    assert pinned.get("branch") == "dev"
    assert "channel" not in pinned


def test_cmd_update_enforces_the_pin(repo: Path, monkeypatch) -> None:
    """``hermes update`` (every surface funnels through cmd_update) honours the pin."""
    import hermes_cli.main as main_mod
    import hermes_cli.update_owning_install as owning

    _pin(repo)
    monkeypatch.setattr(main_mod, "PROJECT_ROOT", repo)
    monkeypatch.setattr(owning, "retarget_to_owning_install", lambda _root: None)
    monkeypatch.setattr(main_mod, "_update_preflight_handled", lambda _args: True)

    with pytest.raises(SystemExit):
        main_mod.cmd_update(_args(branch="main"))

    args = _args()
    main_mod.cmd_update(args)
    assert args.branch == "dev"
