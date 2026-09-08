"""Server-side desktop-plugin (``plugin.js``) probe and install.

Parity target: ``apps/desktop/electron/desktop-plugin-install.ts``. These exercise the real
resolution chain — real ``git clone`` from a real on-disk repo into a real temp ``HERMES_HOME``
— because the whole point of the module is filesystem shapes and clone behavior that a mocked
subprocess would not reproduce.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins_cmd_desktop import (
    desktop_plugin_folder_name,
    detect_plugin_components,
    find_desktop_entry,
    install_desktop_plugin,
    probe_plugin_repo,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    return repo


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")


@pytest.fixture
def desktop_repo(tmp_path: Path) -> Path:
    """A desktop-only plugin repo: a bare ``plugin.js``, no agent-plugin manifest."""
    repo = _init_repo(tmp_path / "my-widget")
    (repo / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    (repo / "README.md").write_text("widget\n", encoding="utf-8")
    _commit_all(repo)
    return repo


@pytest.fixture
def agent_repo(tmp_path: Path) -> Path:
    """An agent-only plugin repo: ``plugin.yaml`` + ``__init__.py``, no desktop half."""
    repo = _init_repo(tmp_path / "agent-only")
    (repo / "plugin.yaml").write_text(yaml.safe_dump({"name": "demo-agent", "version": "1.0.0"}), encoding="utf-8")
    (repo / "__init__.py").write_text("def register(*_a, **_k):\n    pass\n", encoding="utf-8")
    _commit_all(repo)
    return repo


# --- detection (port of findDesktopEntry / detectPluginComponents) ----------


def test_desktop_entry_found_at_root_and_under_desktop_subdir(tmp_path: Path):
    root_style = tmp_path / "root-style"
    (root_style / "desktop").mkdir(parents=True)
    (root_style / "plugin.js").write_text("", encoding="utf-8")
    # Root wins over a nested half when both exist, matching findDesktopEntry's order.
    (root_style / "desktop" / "plugin.js").write_text("", encoding="utf-8")

    assert find_desktop_entry(root_style) == (root_style / "plugin.js", ".")

    nested = tmp_path / "nested-style"
    (nested / "desktop").mkdir(parents=True)
    (nested / "desktop" / "plugin.js").write_text("", encoding="utf-8")

    assert find_desktop_entry(nested) == (nested / "desktop" / "plugin.js", "desktop")

    assert find_desktop_entry(tmp_path / "empty") is None


def test_agent_half_needs_manifest_and_init_or_a_portable_manifest(tmp_path: Path):
    # plugin.yaml alone is NOT an agent plugin — __init__.py is what makes it importable.
    lonely_manifest = tmp_path / "lonely"
    lonely_manifest.mkdir()
    (lonely_manifest / "plugin.yaml").write_text("name: lonely\n", encoding="utf-8")
    assert detect_plugin_components(lonely_manifest)["agent"] is False

    native = tmp_path / "native"
    native.mkdir()
    (native / "plugin.yaml").write_text("name: declared-name\n", encoding="utf-8")
    (native / "__init__.py").write_text("", encoding="utf-8")
    detected = detect_plugin_components(native)
    assert detected["agent"] is True
    # The declared name wins over the directory name.
    assert detected["agentName"] == "declared-name"

    portable = tmp_path / "portable"
    portable.mkdir()
    (portable / "plugin.json").write_text(json.dumps({"name": "portable-name"}), encoding="utf-8")
    assert detect_plugin_components(portable)["agentName"] == "portable-name"


def test_install_folder_name_never_collapses_to_a_generic_desktop_folder():
    # A `#desktop` subdir must not install as a folder literally named "desktop"
    # — every such plugin would collide on one directory.
    assert desktop_plugin_folder_name("https://example.com/owner/my-widget.git", "desktop") == "my-widget"
    assert desktop_plugin_folder_name("https://example.com/owner/repo.git", "packages/thing") == "thing"
    assert desktop_plugin_folder_name("https://example.com/owner/repo.git", None) == "repo"


# --- probe -----------------------------------------------------------------


def test_probe_reports_a_desktop_only_repo(desktop_repo: Path):
    result = probe_plugin_repo(f"file://{desktop_repo}")

    assert result["ok"] is True
    assert result["desktop"] is True
    assert result["agent"] is False
    assert result["desktopName"] == "my-widget"
    # A file:// source is usable but flagged, exactly as the agent installer flags it.
    assert result["insecure"] is True
    assert result["warnings"]


def test_probe_reports_an_agent_only_repo_as_desktop_false_without_erroring(agent_repo: Path):
    result = probe_plugin_repo(f"file://{agent_repo}")

    assert result["ok"] is True
    assert result["agent"] is True
    assert result["desktop"] is False
    assert result["desktopName"] is None
    assert "error" not in result


def test_probe_returns_an_error_result_rather_than_raising(tmp_path: Path):
    # An unclonable source and a malformed identifier both come back as data:
    # the bridge member has no exception channel to the renderer.
    missing = probe_plugin_repo(f"file://{tmp_path / 'nope'}")
    assert missing["ok"] is False
    assert missing["error"]

    malformed = probe_plugin_repo("not-a-repo")
    assert malformed["ok"] is False
    assert malformed["error"]


def test_probe_rejects_a_repo_with_neither_half(tmp_path: Path):
    repo = _init_repo(tmp_path / "empty-repo")
    (repo / "README.md").write_text("nothing here\n", encoding="utf-8")
    _commit_all(repo)

    result = probe_plugin_repo(f"file://{repo}")

    assert result["ok"] is False
    assert result["agent"] is False
    assert result["desktop"] is False
    assert "No agent or desktop plugin artifacts" in result["error"]


# --- install ---------------------------------------------------------------


def test_install_places_the_desktop_half_under_the_plugins_root(desktop_repo: Path, tmp_path: Path):
    root = tmp_path / "home" / "desktop-plugins"
    root.mkdir(parents=True)

    result = install_desktop_plugin(f"file://{desktop_repo}", force=False, desktop_plugins_root=root)

    assert result["ok"] is True
    assert result["pluginName"] == "my-widget"
    assert Path(result["path"]) == root / "my-widget"
    # The loader evaluates this exact file, so its presence is the contract.
    assert (root / "my-widget" / "plugin.js").is_file()


def test_install_copies_only_the_nested_desktop_half(tmp_path: Path):
    repo = _init_repo(tmp_path / "combo")
    (repo / "plugin.yaml").write_text("name: combo\n", encoding="utf-8")
    (repo / "__init__.py").write_text("", encoding="utf-8")
    (repo / "desktop").mkdir()
    (repo / "desktop" / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    _commit_all(repo)

    root = tmp_path / "desktop-plugins"
    root.mkdir()

    result = install_desktop_plugin(f"file://{repo}", force=False, desktop_plugins_root=root)

    assert result["ok"] is True
    assert (root / "combo" / "plugin.js").is_file()
    # The agent half stays out of the desktop root — it belongs in <HERMES_HOME>/plugins.
    assert not (root / "combo" / "plugin.yaml").exists()


def test_install_refuses_to_replace_an_existing_plugin_without_force(desktop_repo: Path, tmp_path: Path):
    root = tmp_path / "desktop-plugins"
    (root / "my-widget").mkdir(parents=True)
    (root / "my-widget" / "plugin.js").write_text("// user's existing copy\n", encoding="utf-8")

    blocked = install_desktop_plugin(f"file://{desktop_repo}", force=False, desktop_plugins_root=root)

    assert blocked["ok"] is False
    assert "already exists" in blocked["error"]
    # Refusal must not have touched the installed copy.
    assert (root / "my-widget" / "plugin.js").read_text(encoding="utf-8") == "// user's existing copy\n"

    forced = install_desktop_plugin(f"file://{desktop_repo}", force=True, desktop_plugins_root=root)

    assert forced["ok"] is True
    assert "user's existing copy" not in (root / "my-widget" / "plugin.js").read_text(encoding="utf-8")


def test_install_of_an_agent_only_repo_reports_the_missing_desktop_half(agent_repo: Path, tmp_path: Path):
    root = tmp_path / "desktop-plugins"
    root.mkdir()

    result = install_desktop_plugin(f"file://{agent_repo}", force=False, desktop_plugins_root=root)

    assert result["ok"] is False
    assert "No desktop plugin.js" in result["error"]
    assert list(root.iterdir()) == []


def test_install_never_writes_outside_the_plugins_root(tmp_path: Path):
    """A subdir that legally resolves back to the clone root must not yield a ``..`` folder name.

    ``sub/..`` passes ``_resolve_subdir_within`` (it lands exactly on the clone root, which is
    permitted) while its last path component is ``..`` — so the folder name derived from it would
    target the plugins root's PARENT, and a forced install would ``rmtree`` it.
    """
    repo = _init_repo(tmp_path / "escape")
    (repo / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "keep.txt").write_text("x\n", encoding="utf-8")
    _commit_all(repo)

    root = tmp_path / "home" / "desktop-plugins"
    root.mkdir(parents=True)
    sibling = root.parent / "config.yaml"
    sibling.write_text("untouched\n", encoding="utf-8")

    result = install_desktop_plugin(f"file://{repo}#sub/..", force=True, desktop_plugins_root=root)

    assert result["ok"] is False
    assert sibling.read_text(encoding="utf-8") == "untouched\n"
    assert root.is_dir()


def test_a_symlink_in_the_cloned_repo_stays_a_symlink(tmp_path: Path):
    """Electron's ``fsp.cp(..., {recursive: true})`` preserves links; dereferencing would copy
    an outside file's BYTES into the plugins root, turning a repo that merely names a path into
    one that exfiltrates its contents.
    """
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("private\n", encoding="utf-8")

    repo = _init_repo(tmp_path / "linky")
    (repo / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    (repo / "leak.txt").symlink_to(secret)
    _commit_all(repo)

    root = tmp_path / "desktop-plugins"
    root.mkdir()

    result = install_desktop_plugin(f"file://{repo}", force=False, desktop_plugins_root=root)

    assert result["ok"] is True
    installed = root / "linky" / "leak.txt"
    assert installed.is_symlink()
    assert not installed.exists() or installed.resolve() == secret.resolve()
    # The decisive assertion: the outside bytes are not sitting in the plugins root as a
    # regular file, which is what symlinks=False produced.
    assert not (installed.is_file() and not installed.is_symlink())


def test_a_symlinked_desktop_source_directory_is_refused(tmp_path: Path):
    """``copytree`` walks the source's ENTRIES, so a symlinked ``desktop/`` half would have an
    outside directory's contents copied in as real files even with ``symlinks=True``."""
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "plugin.js").write_text("// outside\n", encoding="utf-8")
    (outside / "secret.txt").write_text("private\n", encoding="utf-8")

    repo = _init_repo(tmp_path / "linked-half")
    (repo / "desktop").symlink_to(outside, target_is_directory=True)
    _commit_all(repo)

    root = tmp_path / "desktop-plugins"
    root.mkdir()

    result = install_desktop_plugin(f"file://{repo}", force=False, desktop_plugins_root=root)

    assert result["ok"] is False
    assert "symlink" in result["error"]
    assert list(root.iterdir()) == []


def test_force_over_a_destination_symlink_unlinks_it_instead_of_writing_through(tmp_path: Path):
    """``is_dir()`` follows a symlink, ``rmtree`` refuses to remove one, and
    ``ignore_errors=True`` used to hide that — so a forced install reported ``ok`` while writing
    ``plugin.js`` into whatever directory the link pointed at.
    """
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "keep.txt").write_text("untouched\n", encoding="utf-8")

    repo = _init_repo(tmp_path / "my-widget")
    (repo / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    _commit_all(repo)

    root = tmp_path / "desktop-plugins"
    root.mkdir()
    (root / "my-widget").symlink_to(outside, target_is_directory=True)

    result = install_desktop_plugin(f"file://{repo}", force=True, desktop_plugins_root=root)

    assert result["ok"] is True
    assert not (outside / "plugin.js").exists(), "the install wrote through the symlink"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "untouched\n"
    installed = root / "my-widget"
    assert not installed.is_symlink()
    assert (installed / "plugin.js").is_file()


def test_a_destination_symlink_still_blocks_an_unforced_install(tmp_path: Path):
    """An occupied path is occupied whether it is a directory or a link to one — including a
    DANGLING link, which ``is_dir()``/``is_file()`` both report as absent."""
    repo = _init_repo(tmp_path / "my-widget")
    (repo / "plugin.js").write_text("export function activate() {}\n", encoding="utf-8")
    _commit_all(repo)

    root = tmp_path / "desktop-plugins"
    root.mkdir()
    (root / "my-widget").symlink_to(tmp_path / "does-not-exist", target_is_directory=True)

    result = install_desktop_plugin(f"file://{repo}", force=False, desktop_plugins_root=root)

    assert result["ok"] is False
    assert "already exists" in result["error"]
    assert (root / "my-widget").is_symlink()
