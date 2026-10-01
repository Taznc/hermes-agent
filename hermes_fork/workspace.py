"""Per-workspace ``agent_workspace`` for memory-provider bank scoping (tier T1, see FORK.md).

Upstream ``agent_init._memory_provider_init_kwargs`` hands every memory provider the
literal ``agent_workspace="hermes"``, so a provider's ``bank_id_template`` placeholder
``{workspace}`` (hindsight, honcho) can never scope a bank per project: every session
of a profile shares one bank. This module derives the identity from the session's
working directory instead.

Re-implements upstream PR NousResearch/hermes-agent#114452 (its
``agent.runtime_cwd.resolve_workspace_identity`` cascade) in fork-owned code; drop
this module and the anchor when that PR merges.

Config keys (``config.yaml``, read from the resolved ``memory`` section)::

    memory:
      workspace_from_cwd: true      # default false -> upstream behaviour, literal "hermes"
      workspace_default: "hermes"   # what non-workspace sessions resolve to (default "hermes")
      workspace_prefix: "project-"  # prepended to a RESOLVED identity only (default "")

Resolution when the flag is on (never raises; any failure -> ``workspace_default``):

* cwd: ``agent.session_cwd``; else the durable session row's ``cwd``; else the
  runtime session override (``agent.runtime_cwd._SESSION_CWD``: an explicitly-empty
  override means "bound to no cwd" and yields the default); else, only when no
  session override is installed at all (local CLI build), ``TERMINAL_CWD`` then
  ``os.getcwd()``.
* identity cascade:
  1. the declared Hermes project owning cwd (``$HERMES_HOME/projects.db``) -> its slug;
  2. the git repository root's name: the session row's ``git_repo_root`` when present,
     else a bounded ``.git`` walk-up (no subprocess). A ``.git`` FILE (linked worktree,
     ``gitdir: <main>/.git/worktrees/<name>``) resolves to the MAIN repo's toplevel name,
     so Kanban worktrees (``<repo>/.worktrees/<task>`` or
     ``$HERMES_HOME/kanban/boards/<board>/workspaces/<task>``) share the project's bank.
     The ``.git`` check runs BEFORE the ``$HERMES_HOME`` exclusion for that reason;
     a plain ``.git`` directory under ``$HERMES_HOME`` is still non-workspace;
  3. the session row's ``git_repo_root`` hint (Desktop-stamped) when cwd itself is not
     inside a repo;
  4. everything else -> ``workspace_default``: never-a-workspace paths (``/``, ``$HOME``,
     ``dirname($HOME)``, ``/home``, ``/Users``, anything under ``$HERMES_HOME``) AND plain
     directories with no project and no git root (departure from #114452, which uses the
     cwd basename there: an ad-hoc folder must not mint its own bank).

Why a literal default and not ``""``: hindsight's ``_resolve_bank_id_template`` collapses
the ``-``/``_`` run an empty placeholder leaves, so ``project-{workspace}`` would render
``project`` for home-dir sessions (not the static ``bank_id``). Recommended pairing:
``bank_id_template: "{workspace}"`` + ``workspace_prefix: "project-"`` + ``workspace_default:
"hermes"`` -> repo sessions ``project-<repo>``, everything else the existing ``hermes`` bank.

Called from exactly one site: the ``memory-workspace-identity`` FORK ANCHOR in
``agent.agent_init._memory_provider_init_kwargs``. ``mem_config`` is not in scope
there, so when it is not passed this module reads the same ``memory`` section via
``tools.memory_tool.get_builtin_memory_config`` (``load_config_readonly``).
"""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

CONFIG_KEY_ENABLED = "workspace_from_cwd"
CONFIG_KEY_DEFAULT = "workspace_default"
CONFIG_KEY_PREFIX = "workspace_prefix"
UPSTREAM_WORKSPACE = "hermes"

_GIT_WALK_MAX_DEPTH = 12
_UNSET_SENTINEL: Any = object()


# ── config ────────────────────────────────────────────────────────────────────


def _load_mem_config() -> Mapping[str, Any]:
    with suppress(Exception):
        from tools.memory_tool import get_builtin_memory_config
        cfg = get_builtin_memory_config()
        if isinstance(cfg, Mapping):
            return cfg
    return {}


def _is_truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return False


def _workspace_default(mem_config: Mapping[str, Any]) -> str:
    raw = mem_config.get(CONFIG_KEY_DEFAULT)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return UPSTREAM_WORKSPACE


def _workspace_prefix(mem_config: Mapping[str, Any]) -> str:
    """Prefix applied to a RESOLVED workspace identity only (never to the default).

    With ``bank_id_template: "{workspace}"``, ``workspace_prefix: "project-"`` sends repo
    sessions to ``project-<repo>`` while non-workspace sessions still land in the default
    bank verbatim. Putting the prefix in the template instead (``project-{workspace}``)
    would rename the default too (``project-hermes``), orphaning the main bank."""
    raw = mem_config.get(CONFIG_KEY_PREFIX)
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


# ── path classification ───────────────────────────────────────────────────────


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _hermes_home_key() -> str:
    with suppress(Exception):
        from hermes_constants import get_hermes_home
        return _key(str(get_hermes_home()))
    return ""


def _never_workspace_dirs() -> set[str]:
    """``/``, ``$HOME``, ``dirname($HOME)``, ``/home``, ``/Users`` and ``$HERMES_HOME`` itself."""
    home = os.path.realpath(os.path.expanduser("~"))
    dirs = {_key(p) for p in (os.sep, home, os.path.dirname(home), "/home", "/Users") if p}
    if hh := _hermes_home_key():
        dirs.add(hh)
    return dirs


def _under_hermes_home(key: str) -> bool:
    hh = _hermes_home_key()
    return bool(hh) and (key == hh or key.startswith(hh + os.sep))


def _is_non_workspace(path: str) -> bool:
    try:
        key = _key(path)
    except Exception:
        return False
    return not key or key in _never_workspace_dirs() or _under_hermes_home(key)


def _basename(path: str) -> str:
    name = os.path.basename(str(path).rstrip("/\\"))
    return "" if name in ("", ".", "..") else name


# ── git ───────────────────────────────────────────────────────────────────────


def _main_toplevel_from_gitfile(gitfile: Path) -> Optional[Path]:
    """For a linked-worktree ``.git`` FILE, the MAIN repository's toplevel (parent of the
    ``.git`` directory named in ``gitdir:``), or None when it is not that shape."""
    try:
        text = gitfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("gitdir:"):
            continue
        target = line[len("gitdir:"):].strip()
        if not target:
            return None
        gitdir = Path(os.path.expanduser(target))
        if not gitdir.is_absolute():
            gitdir = gitfile.parent / gitdir
        gitdir = Path(os.path.normpath(str(gitdir)))
        # <main>/.git/worktrees/<name> -> <main>; <main>/.git -> <main>.
        for anc in (gitdir, *gitdir.parents):
            if anc.name == ".git":
                return anc.parent
        return None
    return None


def _git_root_name(start: str) -> Optional[str]:
    """Bounded walk-up for ``.git`` (dir or worktree file). Returns the repository name or
    None. The walk stops at a never-a-workspace dir (a dotfiles repo AT ``$HOME`` is not a
    project); a repo root under ``$HERMES_HOME`` only counts when reached through a
    worktree ``.git`` file that points at a main repository (Kanban workspaces)."""
    try:
        cur = Path(start).resolve()
    except OSError:
        return None
    never = _never_workspace_dirs()
    for _ in range(_GIT_WALK_MAX_DEPTH):
        cur_key = os.path.normcase(str(cur))
        if cur_key in never:
            return None
        marker = cur / ".git"
        try:
            is_file, is_dir = marker.is_file(), marker.is_dir()
        except OSError:
            is_file = is_dir = False
        if is_file:
            main = _main_toplevel_from_gitfile(marker)
            if main is not None and not _is_non_workspace(str(main)):
                return _basename(str(main)) or None
            # Not a worktree pointer we can follow: the worktree dir itself is the repo.
            return None if _under_hermes_home(cur_key) else (_basename(str(cur)) or None)
        if is_dir:
            return None if _under_hermes_home(cur_key) else (_basename(str(cur)) or None)
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


# ── cascade ───────────────────────────────────────────────────────────────────


def _project_slug(path: str) -> str:
    """Arm 1: slug of the declared Hermes project owning *path* (read-only, never creates the DB)."""
    with suppress(Exception):
        from hermes_cli.projects_db import connect_closing, project_for_path, projects_db_path
        if projects_db_path().exists():
            with connect_closing() as conn:
                project = project_for_path(conn, path)
            if project is not None:
                return str(project.slug or "").strip()
    return ""


def resolve_workspace_identity(cwd: str, *, repo_root: str = "") -> str:
    """Workspace identity for *cwd*: project slug, else git repo name (cwd, then the
    ``repo_root`` hint); ``""`` for everything else. Never raises."""
    raw = str(cwd or "").strip()
    if not raw:
        return ""
    try:
        path = os.path.abspath(os.path.expanduser(raw))
    except Exception:
        return ""
    if slug := _project_slug(path):
        return slug
    # Git first: a worktree .git file under $HERMES_HOME still names the main repo.
    with suppress(Exception):
        if (name := _git_root_name(path)) is not None:
            return name
    if _is_non_workspace(path):
        return ""
    root = str(repo_root or "").strip()
    if root and os.path.isdir(root) and not _is_non_workspace(root):
        return _basename(root)
    # Deliberate departure from upstream #114452 (which falls back to the cwd
    # basename): a plain directory with no declared project and no git root is
    # not a project. Falling back to the basename would mint a bank per ad-hoc
    # folder (`~/projects` -> `project-projects`, `~/Downloads` -> ...).
    return ""


# ── cwd sourcing ──────────────────────────────────────────────────────────────


def _session_cwd_override() -> Any:
    """The runtime session cwd override: ``_UNSET_SENTINEL`` when none is installed,
    else the (possibly empty) declared value."""
    with suppress(Exception):
        from agent import runtime_cwd as rt
        value = rt._SESSION_CWD.get()
        if value is rt._UNSET:
            return _UNSET_SENTINEL
        return str(value).strip()
    return _UNSET_SENTINEL


def _agent_cwd(agent: Any) -> tuple[str, str]:
    """``(cwd, repo_root_hint)`` for *agent*; cwd ``""`` means "bound to no workspace"."""
    cwd = str(getattr(agent, "session_cwd", None) or "").strip()
    repo_root = ""
    db = getattr(agent, "_session_db", None)
    if db is not None:
        with suppress(Exception):
            row = db.get_session(getattr(agent, "session_id", None)) or {}
            repo_root = str(row.get("git_repo_root") or "").strip()
            if not cwd:
                cwd = str(row.get("cwd") or "").strip()
    if cwd:
        return cwd, repo_root
    override = _session_cwd_override()
    if override is not _UNSET_SENTINEL:
        return str(override), repo_root
    # No session context at all (local CLI build): the ambient surface cwd is real.
    with suppress(Exception):
        from agent.runtime_cwd import scope_terminal_cwd
        cwd = scope_terminal_cwd().strip()
    if not cwd:
        with suppress(Exception):
            cwd = os.getcwd()
    return cwd, repo_root


# ── entry point ───────────────────────────────────────────────────────────────


def agent_workspace(agent: Any, mem_config: Optional[Mapping[str, Any]] = None) -> str:
    """Value for ``initialize_all(agent_workspace=...)``.

    ``memory.workspace_from_cwd`` off (default) -> ``"hermes"`` (upstream behaviour).
    On -> the session's workspace identity, or ``memory.workspace_default`` when the
    session has no real workspace. Never raises.
    """
    try:
        cfg = mem_config if isinstance(mem_config, Mapping) else _load_mem_config()
        if not _is_truthy(cfg.get(CONFIG_KEY_ENABLED)):
            return UPSTREAM_WORKSPACE
        default = _workspace_default(cfg)
        cwd, repo_root = _agent_cwd(agent)
        identity = resolve_workspace_identity(cwd, repo_root=repo_root)
        return f"{_workspace_prefix(cfg)}{identity}" if identity else default
    except Exception:
        logger.debug("agent_workspace resolution failed; using %r", UPSTREAM_WORKSPACE, exc_info=True)
        return UPSTREAM_WORKSPACE
