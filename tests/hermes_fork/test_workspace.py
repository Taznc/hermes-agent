"""``memory.workspace_from_cwd`` (``hermes_fork.workspace``): per-workspace ``agent_workspace``.

Unit tests drive ``agent_workspace`` with a stub agent (SimpleNamespace); the last test
goes through the real ``agent_init._memory_provider_init_kwargs`` anchor site.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_fork.workspace as ws
from agent import runtime_cwd as rt

_REAL_PROJECT_SLUG = ws._project_slug  # captured before the ``isolated`` fixture stubs it


# ── helpers ───────────────────────────────────────────────────────────────────


def _agent(cwd=None, db=None, session_id="sess-ws"):
    return SimpleNamespace(session_cwd=cwd, _session_db=db, session_id=session_id)


class _Db:
    def __init__(self, row):
        self.row = row

    def get_session(self, session_id):
        return self.row


class _BoomDb:
    def get_session(self, session_id):
        raise RuntimeError("db down")


ON = {"workspace_from_cwd": True}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """No projects DB, HERMES_HOME under tmp, HOME under tmp, no runtime session override.

    Note: the walk-up stops at the never-a-workspace dirs, so ``$HOME`` itself may not be a
    workspace parent for git-walk tests; fixtures below build repos under ``tmp_path/work``,
    with HOME pinned to ``tmp_path/home``.
    """
    home = tmp_path / "home"
    home.mkdir()
    hh = home / ".hermes-test"
    hh.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ws, "_hermes_home_key", lambda: ws._key(str(hh)))
    monkeypatch.setattr(ws, "_project_slug", lambda path: "")
    # ContextVar attributes are read-only; install a real value and restore it afterwards.
    token = rt._SESSION_CWD.set(rt._UNSET)
    yield SimpleNamespace(home=home, hermes_home=hh, work=tmp_path / "work")
    rt._SESSION_CWD.reset(token)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    return root


def _worktree(main: Path, wt: Path, name: str) -> Path:
    wt.mkdir(parents=True, exist_ok=True)
    gitdir = main / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return wt


# ── flag off ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cfg", [{}, {"workspace_from_cwd": False}, {"workspace_from_cwd": "no"}, None])
def test_flag_off_returns_upstream_literal(cfg, tmp_path, monkeypatch, isolated):
    monkeypatch.setattr(ws, "_load_mem_config", lambda: {})
    assert ws.agent_workspace(_agent(cwd=str(_repo(isolated.work / "repo"))), cfg) == "hermes"


def test_flag_off_does_not_touch_cwd_sources(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not resolve cwd when the flag is off")

    monkeypatch.setattr(ws, "_agent_cwd", boom)
    assert ws.agent_workspace(_agent(cwd="/tmp/whatever"), {}) == "hermes"


# ── flag on: git ──────────────────────────────────────────────────────────────


def test_git_repo_dir_yields_repo_name(isolated):
    repo = _repo(isolated.work / "my-project")
    assert ws.agent_workspace(_agent(cwd=str(repo)), ON) == "my-project"


def test_subdirectory_of_repo_yields_repo_name(isolated):
    repo = _repo(isolated.work / "my-project")
    sub = repo / "src" / "pkg" / "deep"
    sub.mkdir(parents=True)
    assert ws.agent_workspace(_agent(cwd=str(sub)), ON) == "my-project"


def test_worktree_gitfile_resolves_to_main_repo_name(isolated):
    main = _repo(isolated.work / "hermes-agent-next")
    wt = _worktree(main, main / ".worktrees" / "t_123", "t_123")
    assert ws.agent_workspace(_agent(cwd=str(wt)), ON) == "hermes-agent-next"
    inner = wt / "agent"
    inner.mkdir()
    assert ws.agent_workspace(_agent(cwd=str(inner)), ON) == "hermes-agent-next"


def test_kanban_workspace_under_hermes_home_prefers_main_repo(isolated):
    """``$HERMES_HOME/kanban/boards/<board>/workspaces/<task>`` holds a worktree .git file:
    the main repo's name wins over the HERMES_HOME exclusion."""
    main = _repo(isolated.work / "hermes-agent-next")
    wt = _worktree(main, isolated.hermes_home / "kanban" / "boards" / "b" / "workspaces" / "task-9", "task-9")
    assert ws.agent_workspace(_agent(cwd=str(wt)), ON) == "hermes-agent-next"


def test_plain_git_dir_under_hermes_home_is_not_a_workspace(isolated):
    repo = _repo(isolated.hermes_home / "plugins" / "someplugin")
    assert ws.agent_workspace(_agent(cwd=str(repo)), ON) == "hermes"


def test_gitfile_with_unparseable_content_falls_back_to_dir_name(isolated):
    wt = isolated.work / "odd"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
    assert ws.agent_workspace(_agent(cwd=str(wt)), ON) == "odd"


def test_walk_up_is_bounded(isolated, monkeypatch):
    monkeypatch.setattr(ws, "_GIT_WALK_MAX_DEPTH", 3)
    repo = _repo(isolated.work / "far")
    deep = repo.joinpath(*["d"] * 6)
    deep.mkdir(parents=True)
    # Too deep to find .git within 3 levels -> not a workspace -> default.
    assert ws.agent_workspace(_agent(cwd=str(deep)), ON) == "hermes"


# ── flag on: basename / non-workspace ─────────────────────────────────────────


def test_plain_non_git_dir_yields_default(isolated):
    """Departure from upstream #114452: no project, no git root -> not a workspace,
    so ad-hoc folders never mint their own bank."""
    d = isolated.work / "scratchpad"
    d.mkdir(parents=True)
    assert ws.agent_workspace(_agent(cwd=str(d)), ON) == "hermes"
    assert ws.agent_workspace(_agent(cwd=str(d)), {"workspace_from_cwd": True, "workspace_default": "misc"}) == "misc"


def test_home_dir_yields_default(isolated):
    assert ws.agent_workspace(_agent(cwd=str(isolated.home)), ON) == "hermes"


def test_filesystem_root_yields_default(isolated):
    assert ws.agent_workspace(_agent(cwd="/"), ON) == "hermes"


def test_under_hermes_home_yields_default(isolated):
    d = isolated.hermes_home / "sessions"
    d.mkdir()
    assert ws.agent_workspace(_agent(cwd=str(d)), ON) == "hermes"
    assert ws.agent_workspace(_agent(cwd=str(isolated.hermes_home)), ON) == "hermes"


def test_workspace_default_is_honored(isolated):
    cfg = {"workspace_from_cwd": True, "workspace_default": "home-bank"}
    assert ws.agent_workspace(_agent(cwd=str(isolated.home)), cfg) == "home-bank"
    # Blank/non-string default -> "hermes".
    assert ws.agent_workspace(_agent(cwd="/"), {"workspace_from_cwd": True, "workspace_default": "  "}) == "hermes"
    assert ws.agent_workspace(_agent(cwd="/"), {"workspace_from_cwd": True, "workspace_default": 3}) == "hermes"


def test_workspace_prefix_applies_to_resolved_identity_only(isolated):
    cfg = {"workspace_from_cwd": True, "workspace_prefix": "project-", "workspace_default": "hermes"}
    repo = _repo(isolated.work / "my-project")
    assert ws.agent_workspace(_agent(cwd=str(repo)), cfg) == "project-my-project"
    # The default is a bank name in its own right: never prefixed.
    assert ws.agent_workspace(_agent(cwd=str(isolated.home)), cfg) == "hermes"
    assert ws.agent_workspace(_agent(cwd=str(isolated.hermes_home / "x")), cfg) == "hermes"


def test_workspace_prefix_blank_or_non_string_is_ignored(isolated):
    repo = _repo(isolated.work / "my-project")
    for raw in ("", "   ", None, 7):
        cfg = {"workspace_from_cwd": True, "workspace_prefix": raw}
        assert ws.agent_workspace(_agent(cwd=str(repo)), cfg) == "my-project"


def test_dotfiles_repo_at_home_is_not_a_workspace(isolated):
    (isolated.home / ".git").mkdir()
    assert ws.agent_workspace(_agent(cwd=str(isolated.home)), ON) == "hermes"


# ── flag on: cwd sourcing ─────────────────────────────────────────────────────


def test_session_row_cwd_used_when_agent_has_none(isolated):
    repo = _repo(isolated.work / "from-row")
    assert ws.agent_workspace(_agent(cwd=None, db=_Db({"cwd": str(repo)})), ON) == "from-row"


def test_session_row_git_repo_root_hint_used_for_non_git_cwd(isolated):
    root = isolated.work / "stamped-root"
    root.mkdir(parents=True)
    d = isolated.work / "elsewhere"
    d.mkdir()
    db = _Db({"cwd": str(d), "git_repo_root": str(root)})
    assert ws.agent_workspace(_agent(cwd=None, db=db), ON) == "stamped-root"


def test_agent_session_cwd_beats_session_row(isolated):
    a = _repo(isolated.work / "agent-cwd")
    b = _repo(isolated.work / "row-cwd")
    assert ws.agent_workspace(_agent(cwd=str(a), db=_Db({"cwd": str(b)})), ON) == "agent-cwd"


def test_runtime_override_used_when_no_agent_or_row_cwd(isolated, monkeypatch):
    repo = _repo(isolated.work / "override-repo")
    rt._SESSION_CWD.set(str(repo))  # fixture teardown resets to its token
    assert ws.agent_workspace(_agent(cwd=None, db=_Db({})), ON) == "override-repo"


def test_explicitly_empty_override_means_no_workspace(isolated, monkeypatch):
    """A session bound to no cwd (gateway/API) must not fall through to os.getcwd()."""
    rt._SESSION_CWD.set("")
    repo = _repo(isolated.work / "process-cwd")
    monkeypatch.chdir(repo)
    assert ws.agent_workspace(_agent(cwd=None, db=None), ON) == "hermes"


def test_no_cwd_no_session_falls_back_to_process_cwd(isolated, monkeypatch):
    repo = _repo(isolated.work / "process-cwd")
    monkeypatch.chdir(repo)
    import agent.runtime_cwd as rt_mod
    monkeypatch.setattr(rt_mod, "scope_terminal_cwd", lambda: "")
    assert ws.agent_workspace(_agent(cwd=None, db=None), ON) == "process-cwd"


def test_terminal_cwd_preferred_over_process_cwd_without_session(isolated, monkeypatch):
    repo = _repo(isolated.work / "terminal-cwd")
    other = _repo(isolated.work / "process-cwd")
    monkeypatch.chdir(other)
    import agent.runtime_cwd as rt_mod
    monkeypatch.setattr(rt_mod, "scope_terminal_cwd", lambda: str(repo))
    assert ws.agent_workspace(_agent(cwd=None, db=None), ON) == "terminal-cwd"


def test_session_db_failure_is_swallowed(isolated, monkeypatch):
    repo = _repo(isolated.work / "process-cwd")
    monkeypatch.chdir(repo)
    import agent.runtime_cwd as rt_mod
    monkeypatch.setattr(rt_mod, "scope_terminal_cwd", lambda: "")
    assert ws.agent_workspace(_agent(cwd=None, db=_BoomDb()), ON) == "process-cwd"


# ── flag on: project slug (arm 1) ─────────────────────────────────────────────


def test_declared_project_slug_wins_over_git(isolated, monkeypatch):
    repo = _repo(isolated.work / "git-name")
    monkeypatch.setattr(ws, "_project_slug", lambda path: "declared-slug")
    assert ws.agent_workspace(_agent(cwd=str(repo)), ON) == "declared-slug"


def test_declared_project_via_real_projects_db(tmp_path, monkeypatch, isolated):
    import hermes_cli.projects_db as pdb

    monkeypatch.setattr(ws, "_project_slug", _REAL_PROJECT_SLUG)  # un-stub arm 1
    folder = isolated.work / "declared"
    folder.mkdir(parents=True)
    db_path = tmp_path / "projects.db"
    conn = pdb.connect(db_path=db_path)
    try:
        pdb.create_project(conn, name="Declared", slug="declared-proj", folders=[str(folder)])
    finally:
        conn.close()
    monkeypatch.setattr(pdb, "projects_db_path", lambda: db_path)
    sub = folder / "sub"
    sub.mkdir()
    assert ws.agent_workspace(_agent(cwd=str(sub)), ON) == "declared-proj"


def test_missing_projects_db_is_skipped(tmp_path, monkeypatch, isolated):
    import hermes_cli.projects_db as pdb

    monkeypatch.setattr(ws, "_project_slug", _REAL_PROJECT_SLUG)
    monkeypatch.setattr(pdb, "projects_db_path", lambda: tmp_path / "missing.db")
    repo = _repo(isolated.work / "no-db")
    assert ws.agent_workspace(_agent(cwd=str(repo)), ON) == "no-db"
    assert not (tmp_path / "missing.db").exists()


# ── never raises ──────────────────────────────────────────────────────────────


def test_exception_inside_resolution_returns_hermes(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ws, "_agent_cwd", boom)
    assert ws.agent_workspace(_agent(cwd="/tmp"), ON) == "hermes"


def test_missing_mem_config_is_loaded_from_config(monkeypatch, isolated):
    repo = _repo(isolated.work / "cfg-loaded")
    monkeypatch.setattr(ws, "_load_mem_config", lambda: {"workspace_from_cwd": True})
    assert ws.agent_workspace(_agent(cwd=str(repo)), None) == "cfg-loaded"
    monkeypatch.setattr(ws, "_load_mem_config", lambda: {})
    assert ws.agent_workspace(_agent(cwd=str(repo)), None) == "hermes"


# ── anchor site ───────────────────────────────────────────────────────────────


def test_anchor_flows_into_memory_provider_init_kwargs(monkeypatch, isolated):
    from agent.agent_init import _GATEWAY_IDENTITY_PARAMS, _memory_provider_init_kwargs

    repo = _repo(isolated.work / "anchored")
    fake = SimpleNamespace(
        session_id="sess-anchor", _session_db=None, _emit_warning=None, _emit_status=None,
        session_cwd=str(repo), **{f"_{n}": None for n in _GATEWAY_IDENTITY_PARAMS},
    )
    monkeypatch.setattr(ws, "_load_mem_config", lambda: {"workspace_from_cwd": True})
    assert _memory_provider_init_kwargs(fake, "cli")["agent_workspace"] == "anchored"
    monkeypatch.setattr(ws, "_load_mem_config", lambda: {})
    assert _memory_provider_init_kwargs(fake, "cli")["agent_workspace"] == "hermes"
