"""A changed ``hermes plugins update`` / dashboard git update goes live like install and enable do.

Upstream calls :func:`hermes_cli.plugins_activation.activate_plugin_now` after install, enable and a
changed dashboard *catalog* re-pin, but not after ``hermes plugins update`` (git pull or catalog
re-pin) nor after a changed dashboard *git* update. Those paths therefore never reach the
``serve-peer-activation`` anchor, and every running serve backend keeps the old plugin until it
restarts. This module wraps the two update entry points so a CHANGED update activates the plugin;
a no-op or failed update activates nothing.

Reached from ONE upstream site: the ``plugin-update-activation`` FORK ANCHOR in
``hermes_cli/plugins_cmd.py``, which rebinds the facade names that every caller resolves at call
time (the ``plugins`` CLI dispatch table, the dashboard update route, ``plugins_cadence``).

Scope decision: ``cmd_update(..., interactive=False)`` is only the gateway's unattended
``plugins.auto_apply`` (``plugins_cadence``); it keeps upstream behaviour (no activation).
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _snapshot(name: str) -> tuple[Optional[str], Dict[str, Any]]:
    """(installed dir name, install-metadata rows) before the update runs."""
    from hermes_cli import plugins_cmd as pc

    target = pc._user_installed_plugin_dir(name)
    return (target.name if target is not None else None), pc._read_install_metadata()


def _changed_installs(before: tuple[Optional[str], Dict[str, Any]]) -> list[str]:
    """Installed names whose install record moved: the target's row (new ``revision`` / catalog
    ``sha``), or a new row a catalog re-pin renamed it to. The tree digest is no signal: a no-op
    git pull still republishes a tree whose ``.git`` gained FETCH_HEAD."""
    from hermes_cli import plugins_cmd as pc

    name, rows = before
    after = pc._read_install_metadata()
    changed = [name] if name is not None and name in after and after[name] != rows.get(name) else []
    changed += sorted(set(after) - set(rows))
    return changed


def wrap_cmd_update(cmd_update: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(cmd_update)
    def _cmd_update(name: str, *args: Any, **kwargs: Any) -> None:
        if not kwargs.get("interactive", True):
            return cmd_update(name, *args, **kwargs)
        before = _snapshot(name)
        cmd_update(name, *args, **kwargs)  # a failed update exits here (SystemExit) and activates nothing
        changed = _changed_installs(before)
        if not changed:
            return
        from hermes_cli import plugins_cmd as pc
        from hermes_cli.plugins_activation import activate_plugin_now, activation_hint

        console = pc._console()
        for installed in changed:
            console.print(f"[dim]{activation_hint(activate_plugin_now(installed, in_process=False))}[/dim]")

    return _cmd_update


def wrap_dashboard_update(update: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    @functools.wraps(update)
    def _dashboard_update(name: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        before = _snapshot(name)
        result = update(name, *args, **kwargs)
        # Failed, consent pending, or the catalog branch that already ran activate_plugin_now in-process.
        if not result.get("ok") or "gateway_reloaded" in result or not _changed_installs(before):
            return result
        from hermes_cli.plugins_activation import activate_plugin_now

        return {**result, **activate_plugin_now(str(result.get("name") or name))}

    return _dashboard_update


def wrap_update_entry_points(cmd_update: Callable[..., None], dashboard_update: Callable[..., Dict[str, Any]]):
    """The ``plugin-update-activation`` anchor: wrapped ``(cmd_update, dashboard_update_user_plugin)``."""
    return wrap_cmd_update(cmd_update), wrap_dashboard_update(dashboard_update)
