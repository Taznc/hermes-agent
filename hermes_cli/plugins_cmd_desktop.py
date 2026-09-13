"""Desktop-plugin (``plugin.js``) probe and install, server-side.

The Electron shell resolves this locally in ``apps/desktop/electron/desktop-plugin-install.ts``
(``probePluginRepo`` / ``installDesktopPluginFromGit``, wired through ``electron/fs-ipc.ts``).
The web-served desktop build has no main process, so its bridge shim
(``apps/desktop/src/web-bridge-shim.ts``) needs the same two operations over REST — the same
seam ``/api/fs/desktop-plugins-root`` already covers for the *scan* half of the on-disk plugin
door.

``/api/dashboard/agent-plugins/install`` cannot serve this: it clones into the AGENT plugin root
(``<HERMES_HOME>/plugins``) and validates for the agent-plugin shape, so a desktop-only repo (a
bare ``plugin.js``, no ``plugin.yaml``/``__init__.py``) is rejected or mis-installed. Cloning,
credential scrubbing and subdirectory resolution ARE shared — they come from
``hermes_cli.plugins_cmd`` rather than being re-implemented here.

Detection is a small independent port of the TypeScript predicates rather than a server call
from Electron: the shapes are four stable filename literals, and Electron must answer this
question locally, before any backend exists, in the local-spawn topology.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from hermes_cli.plugins_cmd import (
    PluginOperationError,
    _clone_plugin_repo,
    _repo_name_from_url,
    _resolve_git_url,
    _resolve_subdir_within,
)

# Mirrors electron/desktop-plugin-install.ts detectPluginComponents(): a native agent plugin
# is plugin.yaml (or .yml) PLUS __init__.py; a portable one is plugin.json on its own.
_AGENT_MANIFEST_NAMES = ("plugin.yaml", "plugin.yml")
_PORTABLE_MANIFEST_NAME = "plugin.json"
_DESKTOP_ENTRY_NAME = "plugin.js"
# `name: value` on its own line, quoted or not — the same shallow read Electron does rather than
# pulling a YAML parser into the shell.
_YAML_NAME_RE = re.compile(r"^name:\s*['\"]?([^'\"\n]+)['\"]?\s*$", re.MULTILINE)

_INSECURE_SCHEME_WARNING = (
    "This URL uses an insecure or local scheme. Prefer https:// or git@ for production installs."
)


def find_desktop_entry(plugin_root: Path) -> Optional[tuple[Path, str]]:
    """The repo's ``plugin.js`` and the subdir it sits in, or None.

    Root first, then a ``desktop/`` half — so one repo can ship an agent plugin at the root and
    its desktop UI underneath.
    """
    root_entry = plugin_root / _DESKTOP_ENTRY_NAME
    if root_entry.exists():
        return root_entry, "."
    nested_entry = plugin_root / "desktop" / _DESKTOP_ENTRY_NAME
    if nested_entry.exists():
        return nested_entry, "desktop"
    return None


def _agent_name(plugin_root: Path, yaml_manifest: Optional[Path]) -> str:
    """Declared plugin name, falling back to the directory name when unreadable."""
    if yaml_manifest is not None:
        try:
            match = _YAML_NAME_RE.search(yaml_manifest.read_text(encoding="utf-8"))
        except OSError:
            match = None
        if match:
            return match.group(1).strip()
        return plugin_root.name
    try:
        parsed = json.loads((plugin_root / _PORTABLE_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return plugin_root.name
    name = parsed.get("name") if isinstance(parsed, dict) else None
    return name if isinstance(name, str) and name else plugin_root.name


def detect_plugin_components(plugin_root: Path) -> dict[str, Any]:
    """Which halves — agent, desktop — a cloned repo contains, and what each is called."""
    yaml_manifest = next((p for n in _AGENT_MANIFEST_NAMES if (p := plugin_root / n).exists()), None)
    has_init = (plugin_root / "__init__.py").exists()
    has_portable = (plugin_root / _PORTABLE_MANIFEST_NAME).exists()
    agent = (yaml_manifest is not None and has_init) or has_portable

    entry = find_desktop_entry(plugin_root)
    desktop_name = None
    if entry is not None:
        entry_file, source_subdir = entry
        desktop_name = plugin_root.name if source_subdir == "." else entry_file.parent.name

    return {
        "agent": agent,
        "desktop": entry is not None,
        "agentName": _agent_name(plugin_root, yaml_manifest) if agent else None,
        "desktopName": desktop_name,
        "desktopSourceSubdir": entry[1] if entry is not None else None,
    }


def desktop_plugin_folder_name(git_url: str, subdir: Optional[str]) -> str:
    """Stable on-disk folder for a desktop plugin — never the clone temp dir or a bare ``desktop``."""
    if subdir:
        parts = [p for p in re.split(r"[/\\]", subdir) if p and p not in {".", "desktop"}]
        if parts:
            return parts[-1]
    return _repo_name_from_url(git_url)


def _validated_folder_name(git_url: str, subdir: Optional[str]) -> str:
    """The install folder name, refused when it is not a single safe path component.

    ``desktop_plugin_folder_name`` derives the name from caller-supplied text, and this is an
    authenticated HTTP route rather than a local IPC handler: a subdirectory ending in ``..``
    yields ``..`` and would install *over* the plugin root's parent.
    """
    name = desktop_plugin_folder_name(git_url, subdir)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise PluginOperationError(f"Refusing to install a desktop plugin named '{name}'.")
    return name


def _insecure_scheme(git_url: str) -> bool:
    return git_url.startswith(("http://", "file://"))


def _replace_existing_target(target_dir: Path) -> None:
    """Remove whatever occupies *target_dir*, without ever following it out of the plugins root.

    ``Path.is_dir()`` follows symlinks, so a ``<desktop-plugins>/<name>`` symlink pointing at an
    outside directory reads as an existing install; ``shutil.rmtree`` then refuses to remove a
    symlink, and copying into it writes through to the outside directory. ``lstat`` semantics are
    what Electron gets for free — ``fsp.rm(..., {recursive: true, force: true})`` unlinks the link
    itself — so this reproduces them: unlink a link, recurse only into a real directory.

    A removal that fails raises rather than being swallowed: continuing past it is precisely how
    the copy ends up writing through the thing that was supposed to be gone.
    """
    if target_dir.is_symlink() or target_dir.is_file():
        target_dir.unlink()
        return
    if target_dir.is_dir():
        shutil.rmtree(target_dir)


def _plugin_root(clone_root: Path, subdir: Optional[str]) -> Path:
    return _resolve_subdir_within(clone_root, subdir) if subdir else clone_root


def _require_within(root: Path, target: Path) -> None:
    """Refuse a target that does not resolve inside *root*.

    The folder-name guard already rejects a ``..`` component, but the plugins root or its parents
    may themselves be symlinks (a profile home relocated by the operator), so containment is a
    question about the RESOLVED pair, not about the name. Checked both before and after the copy:
    a directory that resolves inside beforehand can only stay inside, and re-checking afterwards
    is what makes an ``ok: true`` a statement about where the bytes actually landed.
    """
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise PluginOperationError(f"Refusing to install outside the desktop plugins directory: {target}")


def probe_plugin_repo(identifier: str) -> dict[str, Any]:
    """Clone *identifier* to a temp dir and report which plugin halves it carries.

    camelCase, matching ``PluginProbeResult`` in ``apps/desktop/src/global.d.ts``: this exists
    only to back the bridge member, and a translation layer is where the two shapes would drift.
    A bad identifier or a failed clone is ``ok: false`` with an ``error``, never an exception —
    Electron's IPC handler never throws and the renderer has no catch around the call.
    """
    warnings: list[str] = []
    insecure = False
    try:
        try:
            git_url, subdir = _resolve_git_url(identifier)
        except ValueError as exc:
            raise PluginOperationError(str(exc)) from exc
        insecure = _insecure_scheme(git_url)
        if insecure:
            warnings.append(_INSECURE_SCHEME_WARNING)

        with tempfile.TemporaryDirectory(prefix="hermes-plugin-probe-") as tmp:
            clone_root = Path(tmp) / "repo"
            _clone_plugin_repo(clone_root, git_url, None)
            detected = detect_plugin_components(_plugin_root(clone_root, subdir))

        if not detected["agent"] and not detected["desktop"]:
            return {
                "ok": False,
                "agent": False,
                "desktop": False,
                "warnings": warnings,
                "insecure": insecure,
                "error": "No agent or desktop plugin artifacts found in this repository.",
            }

        return {
            "ok": True,
            "agent": detected["agent"],
            "desktop": detected["desktop"],
            "agentName": detected["agentName"] or (_repo_name_from_url(git_url) if detected["agent"] else None),
            "desktopName": desktop_plugin_folder_name(git_url, subdir) if detected["desktop"] else None,
            "warnings": warnings,
            "insecure": insecure,
        }
    except PluginOperationError as exc:
        return {
            "ok": False,
            "agent": False,
            "desktop": False,
            "warnings": warnings,
            "insecure": insecure,
            "error": str(exc),
        }


def install_desktop_plugin(identifier: str, *, force: bool, desktop_plugins_root: Path) -> dict[str, Any]:
    """Install the desktop half of *identifier* into *desktop_plugins_root*.

    camelCase, matching ``DesktopPluginInstallResult`` in ``apps/desktop/src/global.d.ts``.
    The whole source subtree is copied (``.git`` included) exactly as Electron's
    ``fsp.cp(..., {recursive: true})`` does — the clone's origin has already been stripped of
    credentials by ``_clone_plugin_repo`` — and symlinks inside it are reproduced as symlinks
    rather than dereferenced, matching that same call.
    """
    try:
        try:
            git_url, subdir = _resolve_git_url(identifier)
        except ValueError as exc:
            raise PluginOperationError(str(exc)) from exc
        plugin_name = _validated_folder_name(git_url, subdir)

        with tempfile.TemporaryDirectory(prefix="hermes-plugin-install-") as tmp:
            clone_root = Path(tmp) / "repo"
            _clone_plugin_repo(clone_root, git_url, None)
            plugin_root = _plugin_root(clone_root, subdir)
            detected = detect_plugin_components(plugin_root)

            if not detected["desktop"]:
                return {"ok": False, "error": "No desktop plugin.js found in this repository."}

            source_subdir = detected["desktopSourceSubdir"]
            source_dir = plugin_root if source_subdir == "." else plugin_root / source_subdir
            if source_dir.is_symlink():
                # copytree() scandir()s the top-level source, so a symlinked desktop/ half would
                # be dereferenced and an outside directory's CONTENTS copied in as real files.
                # Electron never produces this from a git clone either; fail closed rather than
                # invent a semantics for it.
                return {"ok": False, "error": "The desktop plugin source directory is a symlink."}

            target_dir = desktop_plugins_root / plugin_name
            target_entry = target_dir / _DESKTOP_ENTRY_NAME

            # lexists: a symlink at the install path counts as occupied even when it dangles,
            # and is.dir()/is_file() would follow it out of the root (see _replace_existing_target).
            if target_dir.is_symlink() or target_dir.is_dir() or target_entry.is_file():
                if not force:
                    return {
                        "ok": False,
                        "error": (
                            f"Desktop plugin '{plugin_name}' already exists. "
                            "Enable force reinstall to replace it."
                        ),
                    }
                _replace_existing_target(target_dir)

            desktop_plugins_root.mkdir(parents=True, exist_ok=True)
            _require_within(desktop_plugins_root, target_dir)
            # symlinks=True is Electron's `fsp.cp(..., {recursive: true})` semantics: a symlink in
            # the cloned repo is reproduced as a symlink, never dereferenced into a real file
            # holding an outside path's bytes.
            shutil.copytree(source_dir, target_dir, symlinks=True, dirs_exist_ok=True)
            # The copy created the tree; re-check now that it resolves, so a success is never
            # reported for bytes that landed outside the plugins root.
            _require_within(desktop_plugins_root, target_dir)

        if not target_entry.is_file():
            return {"ok": False, "error": f"Install completed but {target_entry} is missing."}

        return {"ok": True, "pluginName": plugin_name, "path": str(target_dir)}
    except PluginOperationError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"Desktop plugin install failed: {exc}"}
