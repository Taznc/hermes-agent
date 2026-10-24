"""A real CLI under a borrowed data root loads the owner's dependencies.

A test harness, CI job or per-task home runs an installed ``hermes`` with ``HOME`` and
``HERMES_HOME`` pointed at a fresh directory. That root holds no dependency state for the
checkout, so activation used to fall through to the bare store Python and the first third-party
import failed (``ModuleNotFoundError: ruamel``). The launcher already execs the owner's
interpreter; activation now takes the owner's committed environment with it, while runtime
state (cron jobs here) stays in the borrowed home.

Layout: a release-style checkout that does not sit in its data root (``<tmp>/releases/<sha>``),
published with its own ``.hermes/bin/hermes`` launcher by the owner ``<tmp>/owner``. The owner's
committed environment reaches this test environment's packages through a ``.pth`` file, so the
child runs the real launcher, bootstrap and cron CLI with no mocks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

import pytest

from pm.environments import install_key

pytestmark = pytest.mark.platforms("posix")  # shell launcher and a POSIX venv layout

REPO = Path(__file__).resolve().parents[2]
_NOT_LINKED = {".git", ".hermes", ".venv", "venv", "hermes_bootstrap.py"}


def _release(tmp_path: Path) -> Path:
    """A checkout of this tree outside any data root; its bootstrap is its own file, so the
    bootstrap resolves the checkout (and its install key) here rather than in the repo."""
    root = tmp_path / "releases" / "abc123"
    root.mkdir(parents=True)
    for entry in REPO.iterdir():
        if entry.name not in _NOT_LINKED:
            (root / entry.name).symlink_to(entry)
    shutil.copy2(REPO / "hermes_bootstrap.py", root / "hermes_bootstrap.py")
    return root


def _owner(tmp_path: Path, root: Path) -> Path:
    """The data root that installed *root*: a store Python and a committed environment."""
    owner = tmp_path / "owner"
    python = owner / "tools" / "python-owner" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    base = Path(sys.base_prefix) / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    python.write_text(f"#!/bin/sh\nexec {str(base)!r} \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)
    (owner / "tools" / "facts.json").write_text(json.dumps(
        {"schema": 1, "packages": {"python": {"version": "fixture", "entry": "python-owner"}}}), encoding="utf-8")

    state = owner / "installs" / install_key(root)
    venv = state / "environments" / "gen-1" / "venv"
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    site = venv / "lib" / f"python{version}" / "site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\nversion = {version}.0\n", encoding="utf-8")
    (site / "owner-deps.pth").write_text(sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
    (state / "facts.json").write_text(json.dumps(
        {"schema": 1, "packages": {"venv": {"environment": str(venv), "stamp": "fixture"}}}), encoding="utf-8")
    return owner


def _publish_launcher(root: Path, owner: Path, monkeypatch) -> Path:
    from hermes_cli import _launchers

    monkeypatch.setenv("HERMES_HOME", str(owner))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    _launchers.ensure_install_launchers(root, root / ".hermes" / "bin")
    launcher = root / ".hermes" / "bin" / "hermes"
    assert str(owner / "tools" / "python-owner") in launcher.read_text(encoding="utf-8")
    return launcher


@pytest.mark.parametrize("borrowed_state", ["none", "maintenance-only"])
def test_cron_runs_under_a_fresh_home_and_writes_only_there(tmp_path, monkeypatch, borrowed_state):
    """``maintenance-only``: the home's first launch already recorded boot maintenance for the
    checkout (``installs/<key>/bootstrap``), which makes the state dir exist with no environment."""
    root = _release(tmp_path)
    owner = _owner(tmp_path, root)
    launcher = _publish_launcher(root, owner, monkeypatch)
    home = tmp_path / "fresh" / "home"
    (home / ".hermes").mkdir(parents=True)
    borrowed = home / ".hermes" / "installs" / install_key(root)
    if borrowed_state == "maintenance-only":
        (borrowed / "bootstrap").mkdir(parents=True)
    env = {key: value for key, value in os.environ.items()
           if key not in ("HERMES_RUNTIME_DIR", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"))

    def hermes(*argv: str) -> subprocess.CompletedProcess:
        result = subprocess.run([str(launcher), *argv], env=env, cwd=tmp_path, capture_output=True,
                                text=True, timeout=180, check=False)
        assert result.returncode == 0, f"hermes {' '.join(argv)}\n{result.stdout}\n{result.stderr}"
        assert "ModuleNotFoundError" not in result.stderr
        return result

    hermes("cron", "list")
    hermes("cron", "create", "0 8 * * *", "borrowed-home probe", "--name", "borrowed-probe")
    assert "borrowed-probe" in hermes("cron", "list").stdout

    jobs = json.loads((home / ".hermes" / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert [job["name"] for job in jobs["jobs"]] == ["borrowed-probe"]
    assert not (owner / "cron").exists(), "runtime state leaked into the owning root"
    assert not (borrowed / "facts.json").exists() and not (borrowed / "environments").exists(), (
        "the borrowed home built a dependency environment of its own")
