"""/api/fs/list reports a symlink's target type, matching Electron's readDirForIpc.

The web-served desktop discovers disk plugins through this endpoint and keeps
only ``isDirectory`` entries, so a plugin installed as
``desktop-plugins/<name> -> <checkout>`` must list as a directory.
"""

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client():
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


@pytest.mark.require_symlinks
def test_fs_list_reports_symlink_target_type(client, tmp_path):
    outside = tmp_path / "checkout"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}")
    real_file = tmp_path / "note.txt"
    real_file.write_text("n")

    root = tmp_path / "desktop-plugins"
    root.mkdir()
    (root / "real-dir").mkdir()
    (root / "real-file.txt").write_text("f")
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    (root / "linked-file").symlink_to(real_file)
    (root / "broken").symlink_to(tmp_path / "missing")
    (root / "loop").symlink_to(root / "loop")

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    kinds = {entry["name"]: entry["isDirectory"] for entry in response.json()["entries"]}
    assert kinds == {
        "real-dir": True,
        "linked-dir": True,
        "real-file.txt": False,
        "linked-file": False,
        "broken": False,
        "loop": False,
    }


@pytest.mark.require_symlinks
def test_fs_list_symlinked_dir_is_listable_and_sorted_with_dirs(client, tmp_path):
    outside = tmp_path / "checkout"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}")
    root = tmp_path / "desktop-plugins"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "z-plugin").symlink_to(outside, target_is_directory=True)

    entries = client.get("/api/fs/list", params={"path": str(root)}).json()["entries"]

    assert [entry["name"] for entry in entries] == ["z-plugin", "a.txt"]
    linked = client.get("/api/fs/list", params={"path": entries[0]["path"]}).json()["entries"]
    assert [entry["name"] for entry in linked] == ["manifest.json"]
