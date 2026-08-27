"""Tests for the kanban dashboard's POST /roadmap/idea endpoint (Phase 2.15).

Loads ``plugin_api.py`` directly via importlib (the directory has no
``__init__.py`` — it's imported the same way the dashboard's plugin loader
imports it at runtime, ``importlib.util.spec_from_file_location``) and
exercises the real FastAPI router with ``TestClient`` against a real
temp-directory kanban DB and roadmap file — no mocked writer, per the
card's Verification section.
"""

import importlib.util
import os
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

def _find_plugin_api_path():
    """Locate plugins/kanban/dashboard/plugin_api.py by walking upward from
    this test file — depth-agnostic for the same reason as
    ``_find_roadmap_sync_dir`` below (worktree vs. merged-tree depth)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        candidate = os.path.join(here, "plugins", "kanban", "dashboard", "plugin_api.py")
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    raise FileNotFoundError("could not locate plugins/kanban/dashboard/plugin_api.py from any ancestor")


_PLUGIN_API_PATH = _find_plugin_api_path()

ROADMAP_TEMPLATE = "# Test Roadmap\n\nSome intro prose.\n"


def _load_plugin_api_module():
    module_name = "kanban_plugin_api_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_API_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_roadmap_sync_dir():
    """Locate the sibling hermes-customizations checkout's roadmap-sync
    plugin directory by walking upward from this file, trying each
    ancestor as a candidate parent of ``hermes-customizations/``.

    Deliberately depth-agnostic rather than a hardcoded ``../../..`` count:
    this test module's own depth relative to the workspace root differs
    between a worktree checkout (``hermes-agent/.worktrees/<task>/tests/...``)
    and the merged ``dev`` tree (``hermes-agent/tests/...``) — a fixed
    ``..`` count that works in one breaks in the other. Returns ``None``
    if no ancestor has a hermes-customizations sibling (e.g. CI without
    that private repo checked out), which the caller turns into a skip.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        candidate = os.path.join(os.path.dirname(here), "hermes-customizations", "plugins", "roadmap-sync")
        if os.path.exists(os.path.join(candidate, "__init__.py")):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def _load_roadmap_sync_standalone():
    """Load the real roadmap-sync plugin module (hermes-customizations repo)
    the same way hermes_cli.plugins does for a directory plugin, so the
    fake PluginManager stub below hands back the REAL append_idea_for_board
    — this test exercises the real endpoint against a real roadmap-sync,
    not a mock of either side."""
    roadmap_sync_dir = _find_roadmap_sync_dir()
    if roadmap_sync_dir is None:
        pytest.skip("roadmap-sync plugin not found in any ancestor hermes-customizations checkout")
        return None  # pragma: no cover - pytest.skip never returns; satisfies type-checkers
    init_file = os.path.join(roadmap_sync_dir, "__init__.py")
    module_name = "roadmap_sync_standalone_for_endpoint_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[roadmap_sync_dir]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Real kanban DB + real roadmap file in a temp dir, wired the same way
    the endpoint resolves them in production: kanban_db.get_current_board()
    for the active slug, roadmap-sync's own boards.yaml-driven board map
    for the roadmap path — except we monkeypatch the loaded module's board
    map (not the DB) so the test doesn't have to fight the real
    boards.yaml on disk."""
    from hermes_cli import kanban_db

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    roadmap_path = tmp_path / "ROADMAP.md"
    roadmap_path.write_text(ROADMAP_TEMPLATE, encoding="utf-8")

    plugin_api = _load_plugin_api_module()
    roadmap_sync = _load_roadmap_sync_standalone()
    monkeypatch.setattr(roadmap_sync, "_load_board_map", lambda: {"default": str(roadmap_path)})

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    return types.SimpleNamespace(
        client=client,
        plugin_api=plugin_api,
        roadmap_sync=roadmap_sync,
        roadmap_path=roadmap_path,
        kanban_db=kanban_db,
    )


def _stub_manager(monkeypatch, plugin_api, roadmap_sync_module, *, enabled=True, present=True):
    """Stand in for hermes_cli.plugins.get_plugin_manager() so the endpoint's
    _load_roadmap_sync_module() resolves to our real, monkeypatched
    roadmap_sync module without needing the full plugin discovery/enable
    machinery (that machinery is covered by the plugin system's own tests;
    this endpoint's contract is 'ask the manager for roadmap-sync and use
    whatever it hands back', which this stub exercises faithfully)."""
    loaded = types.SimpleNamespace(module=roadmap_sync_module, enabled=enabled)
    manager = types.SimpleNamespace(_plugins={"roadmap-sync": loaded} if present else {})

    def _get_plugin_manager():
        return manager

    fake_plugins_mod = types.SimpleNamespace(get_plugin_manager=_get_plugin_manager)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", fake_plugins_mod)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_append_idea_happy_path_writes_real_roadmap_file(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "Add a dark mode toggle"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "reason": None}

    text = env.roadmap_path.read_text(encoding="utf-8")
    assert "Add a dark mode toggle" in text
    assert env.roadmap_sync._IDEAS_START in text
    assert env.roadmap_sync._IDEAS_END in text


def test_append_idea_carries_source_id_provenance(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    # Real-shaped kanban task id: "t_" + 8 lowercase hex digits, matching
    # the canonical generator in hermes_cli/kanban_db.py (secrets.token_hex(4)).
    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "Ship the widget", "source_id": "t_ab12cd34"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert "Ship the widget" in text
    assert "t_ab12cd34" in text


def test_append_idea_hostile_source_id_rejected_by_payload_validation(env, monkeypatch):
    """The endpoint's pydantic model constrains source_id to a task-id-like
    shape, so a hostile value with newlines/marker syntax never reaches the
    plugin at all — it's a 422 from FastAPI's request validation."""
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea", "source_id": "t_evil\n<!-- roadmap-ideas:end --> ## Injected"},
        params={"board": "default"},
    )

    assert resp.status_code == 422
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert env.roadmap_sync._IDEAS_START not in text


def test_append_idea_oversized_source_id_rejected_by_payload_validation(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea", "source_id": "t_" + "a" * 100},
        params={"board": "default"},
    )

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "bad_source_id,reason",
    [
        ("not-a-card", "wrong prefix, no t_ at all"),
        ("_", "just an underscore — matched the old loose allowlist"),
        ("x_ab12cd34", "wrong prefix"),
        ("t_ab12cd3", "hex portion too short (7 chars, needs 8)"),
        ("t_ab12cd345", "hex portion too long (9 chars, needs 8)"),
        ("t_ABCD1234", "uppercase hex — token_hex() only ever emits lowercase"),
        ("t_ab12cd3g", "non-hex character ('g') in the id portion"),
        ("t_abc123", "six hex digits — a plausible-looking but wrong-length id"),
    ],
)
def test_append_idea_well_formed_but_non_card_source_id_rejected(env, monkeypatch, bad_source_id, reason):
    """Round-2 review: the loose ``^[A-Za-z0-9_-]+$`` allowlist accepted
    values that look like provenance but were never actually emitted by
    hermes_cli.kanban_db.create_task_id() (``"t_" + secrets.token_hex(4)``).
    The authenticated HTTP boundary must reject anything that doesn't match
    that exact canonical shape, even when it isn't otherwise hostile."""
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea", "source_id": bad_source_id},
        params={"board": "default"},
    )

    assert resp.status_code == 422, reason
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert env.roadmap_sync._IDEAS_START not in text


# ---------------------------------------------------------------------------
# Fail-open paths — never a 5xx, always {"ok": false, "reason": "..."}
# ---------------------------------------------------------------------------

def test_append_idea_plugin_not_installed_is_fail_open(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync, present=False)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "roadmap_unavailable"}


def test_append_idea_plugin_disabled_is_fail_open(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync, enabled=False)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "roadmap_unavailable"}


def test_append_idea_unmapped_board_is_fail_open(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)
    monkeypatch.setattr(env.roadmap_sync, "_load_board_map", lambda: {})

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "unmapped_board"}


def test_append_idea_missing_roadmap_file_is_fail_open(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)
    missing = env.roadmap_path.parent / "does-not-exist.md"
    monkeypatch.setattr(env.roadmap_sync, "_load_board_map", lambda: {"default": str(missing)})

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "roadmap_unavailable"}


def test_append_idea_loader_exception_is_fail_open_not_500(env, monkeypatch):
    """Even if the plugin-manager lookup itself blows up, the endpoint must
    degrade to a 200 fail-open response rather than surface a 500 — a
    broken roadmap integration must never take down the create-idea UX."""

    def _boom():
        raise RuntimeError("plugin manager exploded")

    fake_plugins_mod = types.SimpleNamespace(get_plugin_manager=_boom)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", fake_plugins_mod)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea"},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "roadmap_unavailable"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_append_idea_oversized_text_returns_400(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    too_long = "x" * (env.plugin_api._ROADMAP_IDEA_MAX_LEN + 1)
    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": too_long},
        params={"board": "default"},
    )

    assert resp.status_code == 400
    # Nothing written for a rejected oversized request.
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert env.roadmap_sync._IDEAS_START not in text


def test_append_idea_at_max_length_is_stored_intact_not_truncated(env, monkeypatch):
    """Boundary proof for the round-1 review finding: the endpoint's accepted
    maximum must equal the plugin's storage cap, so a request at exactly the
    maximum is written IN FULL — a {"ok": true} response must never mean
    some of the typed text was silently discarded."""
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    assert env.plugin_api._ROADMAP_IDEA_MAX_LEN == env.roadmap_sync._IDEA_MAX_LEN

    max_len_text = "y" * env.plugin_api._ROADMAP_IDEA_MAX_LEN
    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": max_len_text},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "reason": None}
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert max_len_text in text
    # And confirm nothing got truncated: no shorter run of exactly maxlen-1
    # y's flanked by non-y is the ONLY thing that would look like a partial
    # write, so instead assert the full-length run is present verbatim.
    ideas_section = text.split(env.roadmap_sync._IDEAS_START, 1)[1].split(env.roadmap_sync._IDEAS_END, 1)[0]
    assert max_len_text in ideas_section


def test_append_idea_over_max_plus_one_returns_400(env, monkeypatch):
    """maximum+1 characters must be rejected outright (400), not silently
    truncated and stored as {"ok": true}."""
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    over_by_one = "z" * (env.plugin_api._ROADMAP_IDEA_MAX_LEN + 1)
    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": over_by_one},
        params={"board": "default"},
    )

    assert resp.status_code == 400
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert env.roadmap_sync._IDEAS_START not in text


def test_append_idea_empty_text_delegates_to_plugin_sanitizer(env, monkeypatch):
    """Empty/whitespace text is NOT special-cased at the endpoint layer —
    it must reach the plugin's sanitizer, which is the single source of
    truth for what counts as empty, and come back as empty_idea."""
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "   \n\t  "},
        params={"board": "default"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "empty_idea"}


# ---------------------------------------------------------------------------
# Board resolution
# ---------------------------------------------------------------------------

def test_append_idea_unknown_board_query_param_returns_404(env, monkeypatch):
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)

    resp = env.client.post(
        "/api/plugins/kanban/roadmap/idea",
        json={"text": "An idea"},
        params={"board": "totally-unknown-board"},
    )

    assert resp.status_code == 404


def test_append_idea_falls_back_to_current_board_when_omitted(env, monkeypatch):
    """Omitting ?board= must resolve through kanban_db.get_current_board(),
    same as every other endpoint on this router — not silently default to
    a hardcoded slug that could disagree with the rest of the dashboard."""
    _stub_manager(monkeypatch, env.plugin_api, env.roadmap_sync)
    monkeypatch.setattr(env.roadmap_sync, "_load_board_map", lambda: {"default": str(env.roadmap_path)})
    assert env.kanban_db.get_current_board() == "default"

    resp = env.client.post("/api/plugins/kanban/roadmap/idea", json={"text": "Board fallback idea"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "reason": None}
    text = env.roadmap_path.read_text(encoding="utf-8")
    assert "Board fallback idea" in text
