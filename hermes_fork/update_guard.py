"""Refuse ``hermes update`` on a checkout that follows a non-``origin`` remote.

The updater always fetches ``origin/<branch>`` (``main`` unless ``--branch``)
and fast-forwards, merges, or ``reset --hard``s onto it. A fork runtime that
keeps upstream as ``origin`` and deploys from a second remote (``fork/dev``)
would silently receive the upstream branch instead of its own: an unreviewed
upstream sync through a button click.

The signal is git's own tracking config: when the current branch's
``branch.<name>.remote`` is set and is not ``origin``, the updater's target is
not what this checkout follows, so refuse before anything is fetched. Checkouts
that track ``origin`` (every stock install, and forks that cloned their own
repo as origin) are untouched.

``HERMES_ALLOW_CROSS_REMOTE_UPDATE=1`` bypasses the guard for one run.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

BYPASS_ENV = "HERMES_ALLOW_CROSS_REMOTE_UPDATE"


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def cross_remote_refusal(root: Path) -> Optional[str]:
    """Refusal message when ``hermes update`` would pull from the wrong remote."""
    if os.environ.get(BYPASS_ENV) == "1":
        return None
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        return None
    remote = _git(root, "config", "--get", f"branch.{branch}.remote")
    if not remote or remote in ("origin", "."):
        return None
    merge = _git(root, "config", "--get", f"branch.{branch}.merge") or f"refs/heads/{branch}"
    tracked = f"{remote}/{merge.removeprefix('refs/heads/')}"
    origin_url = _git(root, "remote", "get-url", "origin") or "origin"
    return (
        f"✗ Refusing to update: this checkout's '{branch}' tracks {tracked}, "
        f"but `hermes update` pulls from origin ({origin_url}).\n"
        f"  Running it would replace your deployed branch with the origin branch.\n"
        f"  Deploy from the tracked remote instead:\n"
        f"    git -C {root} fetch {remote} && "
        f"git -C {root} merge --ff-only {tracked}\n"
        f"  then restart the Hermes services.\n"
        f"  To override for one run: {BYPASS_ENV}=1 hermes update ..."
    )
