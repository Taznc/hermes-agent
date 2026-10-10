"""Pin a fork install's updates to one repository and branch.

Upstream's ``hermes update`` targets ``main`` unless ``--branch`` is passed on
every run, and the Desktop Update button and the gateway ``/update`` command
cannot pass it reliably. Upstream twice declined a configurable default branch
(NousResearch/hermes-agent#44422, #107496), so the fork carries it here.

The pin lives in the checkout's OWN git config, not in ``config.yaml`` or the
environment::

    git -C <install> config hermes-fork.updateBranch dev
    git -C <install> config hermes-fork.updateOrigin https://github.com/Taznc/hermes-agent.git

That makes it per-install (a second, stock install on the same machine is
unaffected) and immune to ``HERMES_HOME``, profile config and environment
variables. On a pinned install:

* every update path (CLI, ``/update``, dashboard, Desktop hand-off) targets the
  pinned branch, and a conflicting ``--branch`` / ``--channel`` / ``--set-channel``
  is refused instead of honoured;
* the update refuses to run unless ``origin`` resolves (after git's own
  ``insteadOf`` rewriting) to the pinned repository;
* git-config and repository-location environment overrides are stripped from
  the update process so they cannot redirect its fetch;
* update-availability checks (CLI banner, Desktop) report against the pin.

An install with no ``hermes-fork.updateBranch`` behaves exactly like upstream.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn, Optional

BRANCH_KEY = "hermes-fork.updateBranch"
ORIGIN_KEY = "hermes-fork.updateOrigin"

# Environment variables that can change which repository, config or object
# store a git child uses. Stripped from a pinned update process.
_GIT_ENV_EXACT = frozenset({
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE",
})
_GIT_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![./])$")


class UpdatePinError(RuntimeError):
    """A pinned install refused an update request."""


@dataclass(frozen=True)
class UpdatePin:
    branch: str
    origin: Optional[str]


def _is_git_override(key: str) -> bool:
    return key in _GIT_ENV_EXACT or key.startswith(_GIT_ENV_PREFIXES)


def clean_git_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) without git override variables."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if _is_git_override(k)]:
        del env[key]
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(root: Path, *args: str, git: str = "git") -> Optional[str]:
    try:
        proc = subprocess.run(
            [git, *args], cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=clean_git_env(),
            stdin=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _repo_config_file(root: Path, git: str = "git") -> Optional[Path]:
    # No filesystem probe here: `git config --file` on a missing file just finds no key.
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", git=git)
    return Path(common) / "config" if common else None


def read_pin(root: Path, *, git: str = "git") -> Optional[UpdatePin]:
    """The install's pin, read from the repository's own config file only.

    ``git config --file`` reads exactly that file (no global/system config, no
    includes, no ``-c``/environment injection), so nothing outside the checkout
    can create, change or remove a pin.
    """
    cfg = _repo_config_file(Path(root), git=git)
    if cfg is None:
        return None
    branch = _git(Path(root), "config", "--file", str(cfg), "--get", BRANCH_KEY, git=git)
    if not branch:
        return None
    if not _BRANCH_RE.match(branch):
        raise UpdatePinError(f"{BRANCH_KEY} = {branch!r} is not a valid branch name.")
    origin = _git(Path(root), "config", "--file", str(cfg), "--get", ORIGIN_KEY, git=git) or None
    return UpdatePin(branch=branch, origin=origin)


def normalize_repo_url(url: str) -> str:
    """Compare-key for a git remote URL: host + path, lowercase, no scheme/user/.git.

    ``https://github.com/Taznc/hermes-agent.git``, ``git@github.com:Taznc/hermes-agent``
    and ``ssh://git@github.com/Taznc/hermes-agent`` all normalize to
    ``github.com/taznc/hermes-agent``.
    """
    value = url.strip()
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!/)(.+)$", value)
    if scp and "://" not in value:
        host, path = scp.group(1), scp.group(2)
    else:
        rest = value.split("://", 1)[1] if "://" in value else value
        rest = rest.rsplit("@", 1)[-1] if "@" in rest.split("/", 1)[0] else rest
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0]
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}".lower()


def effective_origin_url(root: Path, *, git: str = "git") -> Optional[str]:
    """``origin``'s fetch URL after git's ``insteadOf`` rewriting, i.e. what a fetch dials."""
    return _git(Path(root), "ls-remote", "--get-url", "origin", git=git)


def _refuse(message: str) -> NoReturn:
    print(f"✗ Update refused (fork update pin): {message}", file=sys.stderr)
    print(f"  Pin: git config {BRANCH_KEY} / {ORIGIN_KEY} in this install's checkout.", file=sys.stderr)
    raise SystemExit(1)


def enforce_update_pin(args, project_root: Path, *, git: str = "git") -> Optional[UpdatePin]:
    """Apply the pin to a ``hermes update`` invocation; no-op on an unpinned install.

    Called once at the top of ``cmd_update``, before any update work, so every
    entry point (CLI, ``/update``, dashboard, Desktop hand-off) is covered.
    """
    try:
        pin = read_pin(Path(project_root), git=git)
    except UpdatePinError as exc:
        _refuse(str(exc))
    if pin is None:
        return None

    # Strip git override variables for the rest of this process and its children.
    for key in [k for k in os.environ if _is_git_override(k)]:
        del os.environ[key]

    if getattr(args, "channel", None) or getattr(args, "set_channel", None):
        _refuse(f"update channels are disabled; this install follows branch '{pin.branch}'.")
    requested = (getattr(args, "branch", None) or "").strip()
    if requested and requested != pin.branch:
        _refuse(f"--branch {requested} conflicts with the pinned branch '{pin.branch}'.")

    if pin.origin:
        actual = effective_origin_url(Path(project_root), git=git)
        if not actual:
            _refuse("could not resolve the 'origin' remote of this install.")
        if normalize_repo_url(actual) != normalize_repo_url(pin.origin):
            _refuse(f"origin resolves to {actual}, not the pinned {pin.origin}.")

    args.branch = pin.branch
    print(f"→ Update pinned to {pin.origin or 'origin'} @ {pin.branch}")
    return pin


def pinned_check_target(root: Path, branch: Optional[str], channel: Optional[str], *,
                        git: str = "git") -> tuple[Optional[str], Optional[str]]:
    """Branch/channel for an update-availability check: the pin when one exists.

    Returning an explicit branch makes the check skip channel resolution (no
    query to Nous's release server) and report against the pinned branch, which
    is also the branch the Desktop Update button then passes to ``hermes update``.
    """
    try:
        pin = read_pin(Path(root), git=git)
    except UpdatePinError:
        return branch, channel
    if pin is None:
        return branch, channel
    return pin.branch, "main"
