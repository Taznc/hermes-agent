"""Fork ``projects.scan_repos`` RPC + ``hermes_fork.repo_scan`` walk tests
(moved from tests/tui_gateway/test_projects_rpc.py when the feature moved to
``hermes_fork.gateway`` / ``hermes_fork.repo_scan``)."""

from __future__ import annotations

import os
import subprocess

import pytest

import hermes_fork.repo_scan as server_repo_scan
import tui_gateway.server as server


def _call(method, params=None):
    handler = server._methods[method]
    resp = handler(1, params or {})
    assert "error" not in resp, resp.get("error")
    return resp["result"]


@pytest.fixture(autouse=True)
def _fast_git_probe(monkeypatch):
    """Replace real git subprocess probes with a cheap .git-directory check.

    The record/discover RPC paths probe every distinct session cwd in the DB
    with a real ``git`` subprocess; on a warm session DB that made single
    tests take 10-80s. Behavior under test (policy gating, cache merging,
    ranking) only needs root resolution, not real git.
    """
    from tui_gateway import git_probe

    git_probe.invalidate()

    def _fake_run_git(cwd, *_a):
        d = str(cwd)
        while d and d not in ("/", os.path.dirname(d)):
            if os.path.isdir(os.path.join(d, ".git")):
                return d
            d = os.path.dirname(d)
        return ""

    monkeypatch.setattr(git_probe, "run_git", _fake_run_git)
    yield
    git_probe.invalidate()


def test_scan_repos_is_registered_long_handler():
    # The server-side scan walks real disk, so it must never run on the WS
    # reader thread.
    assert "projects.scan_repos" in server._methods
    assert "projects.scan_repos" in server._LONG_HANDLERS


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    return path


def _scan_policy(monkeypatch, roots, *, enabled=True, excludes=()):
    """Point the effective policy at a temp tree (the walk's only boundary)."""
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "desktop": {
                "repo_scan_enabled": enabled,
                "repo_scan_roots": [str(r) for r in roots],
                "repo_scan_exclude_paths": [str(e) for e in excludes],
            }
        },
    )


def test_scan_repos_walks_the_backends_own_disk(monkeypatch, tmp_path):
    """The whole point of Part 2: the SERVER finds the repos, no client crawl.

    A remote desktop cannot enumerate this machine, so nothing is sent in —
    the handler must discover these roots itself and persist them.
    """
    workspace = tmp_path / "projects"
    _init_repo(workspace / "alpha")
    _init_repo(workspace / "beta")
    (workspace / "not-a-repo").mkdir(parents=True)

    _scan_policy(monkeypatch, [workspace])

    result = _call("projects.scan_repos")
    roots = {item["root"] for item in result["repos"]}

    assert result["accepted"] is True
    assert str(workspace / "alpha") in roots
    assert str(workspace / "beta") in roots
    # A plain directory is not a repo.
    assert str(workspace / "not-a-repo") not in roots

    # Persisted, not just returned: a later read serves it from the cache.
    cached = {item["root"] for item in _call("projects.discover_repos")["repos"]}
    assert str(workspace / "alpha") in cached
    assert str(workspace / "beta") in cached


def test_scan_repos_is_confined_to_its_roots(monkeypatch, tmp_path):
    """Server-side walk triggered by a client RPC is a filesystem-reach
    widening — it must never escape the configured roots."""
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    _init_repo(inside / "wanted")
    _init_repo(outside / "unwanted")

    _scan_policy(monkeypatch, [inside])

    roots = {item["root"] for item in _call("projects.scan_repos")["repos"]}

    assert str(inside / "wanted") in roots
    assert str(outside / "unwanted") not in roots


def test_scan_repos_honors_exclusions(monkeypatch, tmp_path):
    workspace = tmp_path / "projects"
    _init_repo(workspace / "kept")
    _init_repo(workspace / "secret" / "hidden")

    _scan_policy(monkeypatch, [workspace], excludes=[workspace / "secret"])

    roots = {item["root"] for item in _call("projects.scan_repos")["repos"]}

    assert str(workspace / "kept") in roots
    assert all("secret" not in root for root in roots)


def test_scan_repos_stops_at_the_repo_root(monkeypatch, tmp_path):
    """A checkout nested inside a repo is that repo's content, not a project."""
    workspace = tmp_path / "projects"
    outer = _init_repo(workspace / "outer")
    _init_repo(outer / "vendored")

    _scan_policy(monkeypatch, [workspace])

    roots = {item["root"] for item in _call("projects.scan_repos")["repos"]}

    assert str(outer) in roots
    assert str(outer / "vendored") not in roots


def test_scan_repos_disabled_policy_records_nothing(monkeypatch, tmp_path):
    """Disabled discovery returns [] before touching the filesystem and drops
    the cached rows for that policy key."""
    workspace = tmp_path / "projects"
    repo = _init_repo(workspace / "alpha")

    scanned = []
    real_scan = server_repo_scan.scan_repos_on_disk

    def _spy(policy, **kwargs):
        scanned.append(policy)
        return real_scan(policy, **kwargs)

    monkeypatch.setattr(server_repo_scan, "scan_repos_on_disk", _spy)

    # Enabled: the spy is live and the walk happens (guards this test against
    # passing trivially because the patch never took effect).
    _scan_policy(monkeypatch, [workspace])
    assert any(
        item["root"] == str(repo) for item in _call("projects.scan_repos")["repos"]
    )
    assert len(scanned) == 1

    _scan_policy(monkeypatch, [workspace], enabled=False)

    result = _call("projects.scan_repos")

    assert result["accepted"] is False
    assert len(scanned) == 1  # disabled: never walked disk again
    assert all(item["root"] != str(repo) for item in result["repos"])


def test_scan_repos_rejects_a_stale_client_policy(monkeypatch, tmp_path):
    """A client echoing a policy the user has since changed must not persist a
    result built from the old one."""
    workspace = tmp_path / "projects"
    repo = _init_repo(workspace / "alpha")

    _scan_policy(monkeypatch, [workspace])

    stale = _call(
        "projects.scan_repos",
        {
            "discovery_policy": {
                "enabled": True,
                "roots": [str(tmp_path / "somewhere-else")],
                "exclude_paths": [],
            }
        },
    )
    assert stale["accepted"] is False
    assert all(item["root"] != str(repo) for item in stale["repos"])

    # The backend's own effective policy, echoed back, is honored.
    matching = _call(
        "projects.scan_repos",
        {"discovery_policy": stale["discovery_policy"]},
    )
    assert matching["accepted"] is True
    assert any(item["root"] == str(repo) for item in matching["repos"])


def test_scan_repos_omitted_policy_uses_the_backends_own(monkeypatch, tmp_path):
    """A remote client cannot know this machine's roots, so sending no policy
    means 'use yours' — not a rejection."""
    workspace = tmp_path / "projects"
    repo = _init_repo(workspace / "alpha")

    _scan_policy(monkeypatch, [workspace])

    result = _call("projects.scan_repos")

    assert result["accepted"] is True
    assert result["discovery_policy"]["roots"] == [str(workspace)]
    assert any(item["root"] == str(repo) for item in result["repos"])


def test_scan_repos_shape_matches_record_repos(monkeypatch, tmp_path):
    """Both siblings answer the same shape so one client path renders either."""
    workspace = tmp_path / "projects"
    _init_repo(workspace / "alpha")

    _scan_policy(monkeypatch, [workspace])

    scanned = _call("projects.scan_repos")
    recorded = _call(
        "projects.record_repos",
        {
            "discovery_policy": scanned["discovery_policy"],
            "repos": [{"root": str(workspace / "alpha")}],
        },
    )

    assert set(scanned) == set(recorded) == {"repos", "accepted", "discovery_policy"}


def test_scan_repos_skips_junk_and_dot_dirs(monkeypatch, tmp_path):
    """node_modules/venv checkouts are vendored noise, not the user's projects."""
    workspace = tmp_path / "projects"
    _init_repo(workspace / "real")
    _init_repo(workspace / "node_modules" / "dep")
    _init_repo(workspace / ".cache" / "thing")

    _scan_policy(monkeypatch, [workspace])

    roots = {item["root"] for item in _call("projects.scan_repos")["repos"]}

    assert str(workspace / "real") in roots
    assert all("node_modules" not in root and ".cache" not in root for root in roots)


def test_scan_repos_empty_roots_mean_the_backends_home(monkeypatch, tmp_path):
    """Mirrors scanGitRepos's `roots.length ? roots : [os.homedir()]`."""
    home = tmp_path / "home"
    repo = _init_repo(home / "work" / "alpha")

    _scan_policy(monkeypatch, [])

    found = server_repo_scan.scan_repos_on_disk(
        {"enabled": True, "roots": [], "exclude_paths": []}, home_dir=str(home)
    )

    assert str(repo) in {root for root, _label in found}


def test_scan_repos_walk_is_depth_bounded(tmp_path):
    """Bounded depth is what keeps a client-triggered server walk cheap."""
    shallow = _init_repo(tmp_path / "a" / "shallow")
    deep = _init_repo(tmp_path / "a" / "b" / "c" / "d" / "e" / "deep")

    found = {
        root
        for root, _label in server_repo_scan.scan_repos_on_disk(
            {"enabled": True, "roots": [str(tmp_path)], "exclude_paths": []}
        )
    }

    assert str(shallow) in found
    assert str(deep) not in found


def test_scan_repos_does_not_follow_symlinks(tmp_path):
    """A symlinked directory must not let the walk escape its roots (or loop)."""
    workspace = tmp_path / "projects"
    _init_repo(workspace / "real")
    elsewhere = _init_repo(tmp_path / "elsewhere" / "linked")
    (workspace / "link").symlink_to(elsewhere.parent, target_is_directory=True)

    found = {
        root
        for root, _label in server_repo_scan.scan_repos_on_disk(
            {"enabled": True, "roots": [str(workspace)], "exclude_paths": []}
        )
    }

    assert str(workspace / "real") in found
    assert all("elsewhere" not in root for root in found)
