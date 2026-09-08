"""HTTP contract of the desktop-plugin probe/install routes.

These back ``window.hermesDesktop.probePluginRepo`` / ``installDesktopPlugin`` in the
web-served desktop build (``apps/desktop/src/web-bridge-shim.ts``). Two things are contract,
not implementation detail, and both are asserted here rather than in
``test_plugins_cmd_desktop.py``:

* the response is **camelCase**, handed to the renderer verbatim as ``PluginProbeResult`` /
  ``DesktopPluginInstallResult`` (``apps/desktop/src/global.d.ts``);
* a user-facing failure is **HTTP 200 with ``ok: false``**, never a 4xx — the shim's ``api()``
  throws on a non-2xx response and ``PluginInstallModal`` calls both members without a catch,
  so a 4xx would strand the dialog in its probing state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.web_routers.dashboard_ui as dashboard_ui
import hermes_cli.web_routers.files as files_router


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    """The dashboard router alone, with auth stubbed and a temp desktop-plugins root."""
    monkeypatch.setattr(dashboard_ui, "_require_token", lambda _request: None)
    root = tmp_path / "hermes-home" / "desktop-plugins"
    monkeypatch.setattr(files_router, "_fs_plugin_root", lambda _dir, _profile: root)

    app = FastAPI()
    app.include_router(dashboard_ui.router)
    test_client = TestClient(app)
    test_client.desktop_plugins_root = root  # type: ignore[attr-defined]
    return test_client


@pytest.fixture
def profile_client(monkeypatch, tmp_path) -> TestClient:
    """Real ``_fs_plugin_root``, real profile resolution, over a temp hermes root.

    Deliberately UNMOCKED: the mocked ``client`` above cannot see whether ``?profile=`` was
    honored, because its stub ignores the argument. This one resolves the named profile through
    the same ``_config_profile_scope`` -> ``get_hermes_home()`` chain
    ``GET /api/fs/desktop-plugins-root`` uses, so install and the post-install discovery scan are
    asserted to land on one root rather than assumed to.
    """
    monkeypatch.setattr(dashboard_ui, "_require_token", lambda _request: None)
    home = tmp_path / ".hermes"
    (home / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    app = FastAPI()
    app.include_router(dashboard_ui.router)
    app.include_router(files_router.router)
    test_client = TestClient(app)
    test_client.hermes_home = home  # type: ignore[attr-defined]
    return test_client


def _desktop_repo(path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)

    path.mkdir(parents=True)
    git("init", "-q")
    git("config", "user.email", "fixture@example.com")
    git("config", "user.name", "Fixture")
    (path / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "fixture")
    return path


def test_probe_reports_a_desktop_repo_in_the_shape_global_d_ts_declares(client, tmp_path):
    repo = _desktop_repo(tmp_path / "my-widget")

    response = client.post("/api/dashboard/plugins/probe", json={"identifier": f"file://{repo}"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["desktop"] is True
    assert body["agent"] is False
    # camelCase, matching PluginProbeResult — a snake_case key here would read
    # as undefined in the renderer and silently disable the desktop checkbox.
    assert body["desktopName"] == "my-widget"
    assert "desktop_name" not in body


def test_install_writes_into_the_profile_aware_desktop_plugins_root(client, tmp_path):
    repo = _desktop_repo(tmp_path / "my-widget")

    response = client.post(
        "/api/dashboard/desktop-plugins/install", json={"identifier": f"file://{repo}", "force": False}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["pluginName"] == "my-widget"
    # The same root /api/fs/desktop-plugins-root reports, so the discovery scan
    # that runs right after an install looks where the install actually wrote.
    root = client.desktop_plugins_root
    assert Path(body["path"]) == root / "my-widget"
    assert (root / "my-widget" / "plugin.js").is_file()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/dashboard/plugins/probe", {"identifier": "not-a-repo"}),
        ("/api/dashboard/desktop-plugins/install", {"identifier": "not-a-repo"}),
    ],
)
def test_a_rejected_identifier_is_200_with_ok_false_not_a_4xx(client, path, payload):
    response = client.post(path, json=payload)

    assert response.status_code == 200, (
        "A non-2xx makes the shim's api() throw, and the install modal has no catch — "
        "the dialog would hang in its probing state instead of showing the error."
    )
    body = response.json()
    assert body["ok"] is False
    assert body["error"]


def test_a_named_profile_installs_into_that_profiles_root_and_the_scan_agrees(profile_client, tmp_path):
    """The whole point of ``?profile=``: install and discovery must name ONE directory.

    Without the shim propagating the active profile, the install landed in the SERVING
    process's home while the loader scanned ``profiles/coder/desktop-plugins`` — the plugin
    installed successfully and then never loaded.
    """
    repo = _desktop_repo(tmp_path / "my-widget")
    home = profile_client.hermes_home

    installed = profile_client.post(
        "/api/dashboard/desktop-plugins/install?profile=coder",
        json={"identifier": f"file://{repo}", "force": False},
    )

    assert installed.status_code == 200
    body = installed.json()
    assert body["ok"] is True

    scanned = profile_client.get("/api/fs/desktop-plugins-root?profile=coder")

    assert scanned.status_code == 200
    scan_root = Path(scanned.json()["path"])
    assert scan_root == home / "profiles" / "coder" / "desktop-plugins"
    assert Path(body["path"]).parent == scan_root
    assert (scan_root / "my-widget" / "plugin.js").is_file()
    # And it did NOT leak into the serving process's own home.
    assert not (home / "desktop-plugins" / "my-widget").exists()
