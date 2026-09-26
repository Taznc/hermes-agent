"""Packaging contract: the fork-owned package ships in the wheel.

Upstream code imports ``hermes_fork`` at FORK ANCHOR call sites (for example
``hermes_cli/kanban_db_dispatch.py`` review-routing, which runs on every
dispatch admission). ``pyproject.toml``'s ``packages.find`` is an explicit
allowlist, so a fork package missing from it builds a wheel whose dispatcher
raises ``ModuleNotFoundError``. Source-tree tests cannot see that, so this test
resolves the package list exactly the way setuptools does for the wheel.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wheel_packages() -> set[str]:
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    return set(find_packages(where=str(REPO_ROOT), include=include))


def test_every_fork_package_is_in_the_wheel():
    fork_pkgs = set(find_packages(where=str(REPO_ROOT), include=["hermes_fork", "hermes_fork.*"]))
    assert "hermes_fork" in fork_pkgs
    missing = sorted(fork_pkgs - _wheel_packages())
    assert not missing, f"fork packages absent from the wheel (pyproject packages.find): {missing}"
