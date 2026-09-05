"""Tests for GET /board/all (plugins/kanban/dashboard/plugin_api.py), the consolidated
multi-board view. Loads the real plugin module (same importlib pattern as the sibling
kanban dashboard tests) against a real temp-directory kanban install with several real
boards, so these exercise actual multi-DB fan-out rather than a mocked aggregator.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_board_all_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin_api(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return _load_plugin_module()


@pytest.fixture
def client(plugin_api):
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _create_board(client, slug, **kwargs):
    r = client.post("/api/plugins/kanban/boards", json={"slug": slug, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()["board"]


def _create_task(client, board, **kwargs):
    payload = {"title": kwargs.pop("title", f"task on {board}")}
    payload.update(kwargs)
    r = client.post(f"/api/plugins/kanban/tasks?board={board}", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["task"]


# ---------------------------------------------------------------------------
# Merge across boards, attribution, dedup
# ---------------------------------------------------------------------------


def test_board_all_merges_with_board_attribution(client):
    _create_board(client, "alpha", name="Alpha Team", color="#ff0000", icon="rocket")
    _create_board(client, "beta", name="Beta Team", color="#00ff00", icon="wrench")

    default_task = _create_task(client, "default", title="default task", assignee="dana", tenant="acme")
    alpha_task = _create_task(client, "alpha", title="alpha task", assignee="alice", tenant="acme")
    beta_task = _create_task(client, "beta", title="beta task", assignee="bob", tenant="widgetco")

    r = client.get("/api/plugins/kanban/board/all")
    assert r.status_code == 200, r.text
    data = r.json()

    ready = next(c for c in data["columns"] if c["name"] == "ready")
    by_id = {(t["board"], t["id"]): t for t in ready["tasks"]}

    assert ("default", default_task["id"]) in by_id
    assert ("alpha", alpha_task["id"]) in by_id
    assert ("beta", beta_task["id"]) in by_id

    assert by_id[("alpha", alpha_task["id"])]["board_name"] == "Alpha Team"
    assert by_id[("beta", beta_task["id"])]["board_name"] == "Beta Team"

    # Union of tenants/assignees, sorted + deduped.
    assert data["tenants"] == sorted(set(data["tenants"]))
    assert {"acme", "widgetco"} <= set(data["tenants"])
    assert {"alice", "bob", "dana"} <= set(data["assignees"])
    assert len(data["assignees"]) == len(set(data["assignees"]))

    # boards list carries per-board metadata + a live task_count.
    boards_by_slug = {b["slug"]: b for b in data["boards"]}
    assert boards_by_slug["alpha"]["name"] == "Alpha Team"
    assert boards_by_slug["alpha"]["color"] == "#ff0000"
    assert boards_by_slug["alpha"]["task_count"] >= 1
    assert boards_by_slug["beta"]["task_count"] >= 1

    assert data["errors"] == []


def test_board_all_task_lands_in_correct_column(client):
    _create_board(client, "gamma")
    task = _create_task(client, "gamma", title="triage me")
    client.patch(f"/api/plugins/kanban/tasks/{task['id']}?board=gamma", json={"status": "blocked", "block_reason": "waiting"})

    r = client.get("/api/plugins/kanban/board/all")
    data = r.json()
    blocked = next(c for c in data["columns"] if c["name"] == "blocked")
    found = [t for t in blocked["tasks"] if t["board"] == "gamma" and t["id"] == task["id"]]
    assert len(found) == 1
    # Must not also appear in some other column.
    for col in data["columns"]:
        if col["name"] == "blocked":
            continue
        assert all(not (t["board"] == "gamma" and t["id"] == task["id"]) for t in col["tasks"])


def test_board_all_link_edges_are_objects_scoped_to_board(client):
    _create_board(client, "delta")
    parent = _create_task(client, "delta", title="parent")
    child = _create_task(client, "delta", title="child", parents=[parent["id"]])

    r = client.get("/api/plugins/kanban/board/all")
    data = r.json()
    edges = [e for e in data["link_edges"] if e["board"] == "delta"]
    assert {"board": "delta", "parent": parent["id"], "child": child["id"]} in edges
    # Objects, not the 2-element-array shape /board still uses.
    assert all(isinstance(e, dict) and set(e) == {"board", "parent", "child"} for e in data["link_edges"])


# ---------------------------------------------------------------------------
# Cursors — one per successfully-fetched board
# ---------------------------------------------------------------------------


def test_board_all_cursors_one_entry_per_board(client):
    _create_board(client, "eps1")
    _create_board(client, "eps2")
    _create_task(client, "eps1", title="t1")

    r = client.get("/api/plugins/kanban/board/all")
    data = r.json()
    for slug in ("default", "eps1", "eps2"):
        assert slug in data["cursors"]
        assert isinstance(data["cursors"][slug], int)
    assert data["cursors"]["eps1"] > 0  # at least one event was recorded


# ---------------------------------------------------------------------------
# Per-board failure isolation
# ---------------------------------------------------------------------------


def test_board_all_isolates_a_failing_board(client, plugin_api, monkeypatch):
    _create_board(client, "healthy")
    _create_board(client, "broken")
    healthy_task = _create_task(client, "healthy", title="ok task")
    _create_task(client, "broken", title="doomed task")

    orig_fetch = plugin_api._fetch_board_payload

    def _boom(slug, **kwargs):
        if slug == "broken":
            raise RuntimeError("database is locked")
        return orig_fetch(slug, **kwargs)

    monkeypatch.setattr(plugin_api, "_fetch_board_payload", _boom)

    r = client.get("/api/plugins/kanban/board/all")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["errors"] == [{"board": "broken", "detail": "database is locked"}]
    assert "broken" not in data["cursors"]
    assert "healthy" in data["cursors"]

    ready = next(c for c in data["columns"] if c["name"] == "ready")
    ids = {(t["board"], t["id"]) for t in ready["tasks"]}
    assert ("healthy", healthy_task["id"]) in ids
    assert all(b != "broken" for b, _ in ids)

    # The broken board still appears in the boards list (just with no tasks), so the UI
    # can render it as present-but-degraded rather than silently missing.
    broken_info = next(b for b in data["boards"] if b["slug"] == "broken")
    assert broken_info["task_count"] == 0


def test_board_all_boards_filter_query_param(client):
    _create_board(client, "keep")
    _create_board(client, "skip")
    keep_task = _create_task(client, "keep", title="keep me")
    _create_task(client, "skip", title="skip me")

    r = client.get("/api/plugins/kanban/board/all?boards=default,keep")
    data = r.json()
    slugs = {b["slug"] for b in data["boards"]}
    assert slugs == {"default", "keep"}
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    ids = {(t["board"], t["id"]) for t in ready["tasks"]}
    assert ("keep", keep_task["id"]) in ids
    assert all(b != "skip" for b, _ in ids)


# ---------------------------------------------------------------------------
# /board response shape is unchanged after the extraction (behavior contract)
# ---------------------------------------------------------------------------


def test_board_single_endpoint_shape_unchanged(client):
    task = _create_task(client, "default", title="solo", assignee="dana", tenant="acme")

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    data = r.json()

    # Same top-level keys as before the extraction — no board/board_name/boards/cursors/errors leak in.
    assert set(data.keys()) == {"columns", "tenants", "assignees", "link_edges", "latest_event_id", "now"}
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    card = next(t for t in ready["tasks"] if t["id"] == task["id"])
    assert "board" not in card
    assert "board_name" not in card
    # link_edges keep the plain 2-element-array shape (not the {board,parent,child} objects
    # introduced for /board/all).
    assert all(isinstance(e, list) and len(e) == 2 for e in data["link_edges"])
    assert isinstance(data["latest_event_id"], int)
