"""`/api/fs/list` must classify a SYMLINKED directory as a directory.

Regression guard for the class of bug where the web build silently loads no
on-disk Desktop plugins.

The renderer's plugin scanner (`apps/desktop/src/contrib/runtime-loader.ts`)
keeps only `entries.filter(e => e.isDirectory)` when walking a plugin root.
The SAME renderer runs against two shells:

  * Electron  -> `readDirForIpc` (apps/desktop/electron/fs-read-dir.ts), which
    deliberately stats symlinked dirents so a link to a directory reports
    `isDirectory: true`;
  * web build -> this `/api/fs/list` route.

`scandir(...).is_dir(follow_symlinks=False)` reported a symlinked plugin
folder as a NON-directory, so the scanner skipped it and the plugin never
loaded — while the identical install worked under Electron. Symlinking a
plugin folder out of a source repo into `<hermes home>/desktop-plugins/` is
the documented dev install, so this broke every symlink-installed plugin on
the web build, with no error surfaced anywhere.

These tests assert the CONTRACT the renderer depends on (the two shells agree
on entry type), not the specific syscall used to satisfy it.
"""

import os

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture(autouse=True)
def _isolate_app_state():
    """Neutralize cross-module app.state leakage.

    `start_server()` (exercised by tests/test_web_server.py) sets
    `app.state.bound_host`, which arms the Host-header guard on the SHARED
    module-level `web_server.app`. Whether that runs before this module is a
    pytest ordering accident, so without this fixture these tests pass alone
    and fail in a full-suite run with an unrelated 400/401 — noise that says
    nothing about symlink classification. Save, clear, restore.
    """
    saved = {}
    for key in ("auth_required", "bound_host", "trusted_public_hosts"):
        if hasattr(web_server.app.state, key):
            saved[key] = getattr(web_server.app.state, key)
            # Starlette's State proxies a plain dict and raises KeyError (not
            # AttributeError) when deleting a key it doesn't hold.
            try:
                delattr(web_server.app.state, key)
            except (AttributeError, KeyError):
                pass

    yield

    for key, value in saved.items():
        setattr(web_server.app.state, key, value)


def _entries(tmp_path):
    # `/api/fs/*` sits behind the dashboard session-token gate; present the
    # server's own token so these tests exercise the real route (and a 401
    # can never masquerade as a passing symlink assertion).
    #
    # Read the token through the MODULE at call time, never captured earlier:
    # a sibling test in this suite rebinds `web_server._SESSION_TOKEN`, so a
    # value snapshotted at import/collection time goes stale and every
    # assertion here degrades into an unrelated 401.
    client = TestClient(web_server.app)
    response = client.get(
        "/api/fs/list",
        params={"path": str(tmp_path)},
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )
    assert response.status_code == 200, response.text
    return {entry["name"]: entry for entry in response.json()["entries"]}


def test_symlinked_directory_is_reported_as_a_directory(tmp_path):
    """A symlink pointing at a directory must look like a directory.

    This is the exact shape of a symlink-installed Desktop plugin:
    <hermes home>/desktop-plugins/<name> -> <source repo>/desktop-plugins/<name>
    """
    real_plugin = tmp_path / "source-repo" / "account-limits"
    real_plugin.mkdir(parents=True)
    (real_plugin / "plugin.js").write_text("export default {}\n", encoding="utf-8")

    root = tmp_path / "desktop-plugins"
    root.mkdir()
    os.symlink(real_plugin, root / "account-limits")

    entry = _entries(root)["account-limits"]

    assert entry["isDirectory"] is True, (
        "a symlinked plugin folder must report isDirectory=True, or "
        "runtime-loader.ts's entries.filter(e => e.isDirectory) drops it and "
        "the plugin silently never loads on the web build"
    )


def test_symlinked_file_is_not_reported_as_a_directory(tmp_path):
    """Following symlinks must not misclassify a link to a FILE."""
    real_file = tmp_path / "real-plugin.js"
    real_file.write_text("export default {}\n", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    os.symlink(real_file, root / "linked-plugin.js")

    assert _entries(root)["linked-plugin.js"]["isDirectory"] is False


def test_broken_symlink_does_not_break_the_listing(tmp_path):
    """A dangling link must degrade to a non-directory, not fail the request.

    Following a broken symlink raises OSError; the route has to absorb that or
    one stale link makes an entire plugin root unlistable — which would take
    out every healthy plugin beside it.
    """
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(tmp_path / "does-not-exist", root / "dangling")
    (root / "real-dir").mkdir()

    entries = _entries(root)

    assert entries["dangling"]["isDirectory"] is False
    # The healthy sibling still lists — the broken link didn't poison the scan.
    assert entries["real-dir"]["isDirectory"] is True


def test_plain_directories_and_files_still_classify_correctly(tmp_path):
    """Baseline: the non-symlink path is unchanged."""
    (tmp_path / "a-dir").mkdir()
    (tmp_path / "a-file.txt").write_text("x", encoding="utf-8")

    entries = _entries(tmp_path)

    assert entries["a-dir"]["isDirectory"] is True
    assert entries["a-file.txt"]["isDirectory"] is False
