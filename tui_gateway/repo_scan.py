"""Server-side git repository discovery for the Desktop Projects sidebar.

The Desktop crawls its own machine through the Electron ``hermes:git:scanRepos``
capability (``apps/desktop/electron/git-repo-scan.ts``). That is the right tool
only when the backend IS this machine: on a remote gateway the client would
write ``/Users/<mac-user>/...`` paths into another host's ``projects.db``.

This module is the backend's own walk, so a remote gateway can discover the
repos on *its* disk. The traversal semantics are a deliberate port of
``git-repo-scan.ts`` — bounded depth, the same junk-directory skip list,
stop descending at a repo root, dedupe by normalized path — so both machines
answer the same question the same way.

The *policy* is not duplicated here: callers resolve it with
``_repo_discovery_policy()`` in ``server.py`` and hand the result in. This
module only walks what that policy already allows.
"""

from __future__ import annotations

import os

# Same bound as the Electron crawl (git-repo-scan.ts DEFAULT_MAX_DEPTH).
DEFAULT_MAX_DEPTH = 3

# Directories that never contain a user's own checkout but do contain thousands
# of vendored ones. Mirrors JUNK_DIRS in git-repo-scan.ts.
JUNK_DIRS = frozenset(
    {"Applications", "Library", "node_modules", "site-packages", "vendor", "venv"}
)


def normalize_scan_path(raw_path: str, *, home_dir: str | None = None) -> str | None:
    """Expand ``~``, resolve relatives against home, normalize separators.

    Port of ``normalizeRepoScanPath``. Returns ``None`` for blank input.
    Symlinks are deliberately NOT resolved: the walk must stay inside the
    literal subtree the policy names.
    """
    home = home_dir if home_dir is not None else os.path.expanduser("~")
    raw = str(raw_path or "").strip()

    if not raw:
        return None

    expanded = raw

    if raw == "~":
        expanded = home
    elif raw.startswith("~/") or raw.startswith("~\\"):
        expanded = os.path.join(home, raw[2:])

    absolute = expanded if os.path.isabs(expanded) else os.path.join(home, expanded)

    return os.path.normpath(absolute)


def _key(path: str) -> str:
    """Comparison form of a normalized path (case-insensitive on Windows)."""
    return os.path.normcase(path)


def path_is_within(candidate: str, parent: str, *, home_dir: str | None = None) -> bool:
    """True when ``candidate`` is ``parent`` or nested under it.

    Port of ``repoScanPathIsWithin``. Compares normalized keys rather than
    calling ``relpath`` so a candidate on another Windows drive can't raise.
    """
    candidate_path = normalize_scan_path(candidate, home_dir=home_dir)
    parent_path = normalize_scan_path(parent, home_dir=home_dir)

    if not candidate_path or not parent_path:
        return False

    candidate_key = _key(candidate_path)
    parent_key = _key(parent_path)

    if candidate_key == parent_key:
        return True

    return candidate_key.startswith(parent_key.rstrip(os.sep) + os.sep)


def _is_repo_root(entries: list[os.DirEntry], directory: str) -> bool:
    """True when ``directory`` holds a readable ``.git/HEAD``.

    The Electron crawl requires both a ``.git`` *directory* and a readable
    ``HEAD`` inside it, so a stray ``.git`` file (worktree/submodule pointer)
    or an unreadable one doesn't mint a phantom repo.
    """
    has_git_dir = any(
        entry.name == ".git" and entry.is_dir(follow_symlinks=False)
        for entry in entries
    )

    if not has_git_dir:
        return False

    return os.access(os.path.join(directory, ".git", "HEAD"), os.R_OK)


def scan_repos_on_disk(
    policy: dict,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    home_dir: str | None = None,
) -> list[tuple[str, str]]:
    """Walk this backend's disk for git repos under ``policy``.

    ``policy`` is an already-resolved repo discovery policy (``enabled``,
    ``roots``, ``exclude_paths``) — see ``_repo_discovery_policy()``.

    Returns ``(root, label)`` pairs shaped for
    ``projects_db.record_discovered_repos``. A disabled policy returns ``[]``
    without touching the filesystem, and empty roots mean this backend's own
    home directory (mirroring ``scanGitRepos``'s ``roots.length ? roots :
    [os.homedir()]``).
    """
    if not policy.get("enabled", True):
        return []

    home = home_dir if home_dir is not None else os.path.expanduser("~")
    depth_limit = max_depth if isinstance(max_depth, int) and max_depth >= 0 else DEFAULT_MAX_DEPTH

    requested_roots = [r for r in (policy.get("roots") or []) if str(r).strip()] or [home]

    search_roots: dict[str, str] = {}
    for root in requested_roots:
        normalized = normalize_scan_path(root, home_dir=home)
        if normalized:
            search_roots.setdefault(_key(normalized), normalized)

    exclusions = []
    for excluded in policy.get("exclude_paths") or []:
        normalized = normalize_scan_path(excluded, home_dir=home)
        if normalized:
            exclusions.append(normalized)

    def _is_excluded(candidate: str) -> bool:
        return any(
            path_is_within(candidate, excluded, home_dir=home) for excluded in exclusions
        )

    found: dict[str, tuple[str, str]] = {}

    def _walk(directory: str, depth: int) -> None:
        if depth > depth_limit or _is_excluded(directory):
            return

        try:
            with os.scandir(directory) as scandir_it:
                entries = list(scandir_it)
        except OSError:
            # Unreadable / vanished / permission-denied: skip, never abort the
            # whole scan for one bad directory.
            return

        if _is_repo_root(entries, directory):
            normalized = normalize_scan_path(directory, home_dir=home)
            if normalized:
                label = os.path.basename(normalized.rstrip(os.sep)) or normalized
                found.setdefault(_key(normalized), (normalized, label))
            # Stop at the repo root: nested checkouts and vendored copies below
            # a real repo are not separate projects.
            return

        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in JUNK_DIRS:
                continue
            # follow_symlinks=False matches Node's Dirent.isDirectory() and is
            # what keeps a symlink cycle from making this walk unbounded.
            if not entry.is_dir(follow_symlinks=False):
                continue
            _walk(os.path.join(directory, name), depth + 1)

    for root in search_roots.values():
        _walk(root, 0)

    return list(found.values())
