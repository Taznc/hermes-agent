"""hermes_fork.borrowed_home_deps: which root a borrowed launch takes dependencies from.

The real-process regression lives in tests/hermes_cli/test_borrowed_home_cli.py; these pin the
owner choice on its own, including the cases that must keep upstream's behaviour.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_fork import borrowed_home_deps
from pm.environments import install_key

pytestmark = pytest.mark.platforms("posix")


def _state(home: Path, root: Path, *, environment: bool = True) -> Path | None:
    state = home / "installs" / install_key(root)
    state.mkdir(parents=True)
    venv = None
    if environment:
        venv = state / "environments" / "gen-1" / "venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("version = 3.14.0\n", encoding="utf-8")
    record = {"packages": {"venv": {"environment": str(venv)}}} if venv else {"packages": {}}
    (state / "facts.json").write_text(json.dumps(record), encoding="utf-8")
    return venv


def _launcher(root: Path, owner: Path) -> None:
    python = owner / "tools" / "python-x" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    bin_dir = root / ".hermes" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "hermes").write_text(f"#!/bin/sh\nexec {python} -I -c 'pass' \"$@\"\n", encoding="utf-8")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / "releases" / "abc"
    root.mkdir(parents=True)
    owner = tmp_path / "owner"
    borrowed = tmp_path / "scratch" / ".hermes"
    borrowed.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(borrowed.parent))
    monkeypatch.setenv("HERMES_HOME", str(borrowed))
    return root, owner, borrowed


def test_a_release_tree_borrower_takes_the_launcher_owners_environment(layout):
    root, owner, _ = layout
    venv = _state(owner, root)
    _launcher(root, owner)
    assert borrowed_home_deps.owner_root(root) == owner
    assert borrowed_home_deps.borrowed_environment(root) == venv.resolve()


def test_no_launcher_and_no_upstream_owner_means_no_borrowing(layout):
    root, owner, _ = layout
    _state(owner, root)
    assert borrowed_home_deps.owner_root(root) is None
    assert borrowed_home_deps.activate_borrowed_dependencies(root) is False


def test_the_launcher_owner_itself_never_borrows(layout, monkeypatch):
    root, owner, _ = layout
    _state(owner, root, environment=False)
    _launcher(root, owner)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    assert borrowed_home_deps.owner_root(root) is None


def test_an_owner_with_nothing_committed_lends_nothing(layout):
    root, owner, _ = layout
    _state(owner, root, environment=False)
    _launcher(root, owner)
    assert borrowed_home_deps.owner_root(root) == owner
    assert borrowed_home_deps.borrowed_environment(root) is None


def test_an_owner_record_outside_its_generations_is_refused(layout):
    root, owner, _ = layout
    _state(owner, root)
    _launcher(root, owner)
    stray = owner / "elsewhere" / "venv"
    stray.mkdir(parents=True)
    (stray / "pyvenv.cfg").write_text("version = 3.14.0\n", encoding="utf-8")
    facts = owner / "installs" / install_key(root) / "facts.json"
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(stray)}}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="outside this install"):
        borrowed_home_deps.borrowed_environment(root)
