"""Kanban worker bootstrap imports come from the runtime, never the task workspace (tier T1, see FORK.md).

Upstream launches every module-form worker as ``sys.executable -m hermes_cli.main`` with
``cwd=<task workspace>``. ``python -m`` puts the current directory at ``sys.path[0]``,
ahead of PYTHONPATH and site-packages, so a workspace that is itself a Hermes checkout
supplies ``hermes_bootstrap``, ``hermes_cli`` and everything they import. When that
checkout is mid-merge (conflict markers in ``hermes_state_sessions.py``) the worker dies
before its first tool call with "invalid decimal literal", although the gateway's own
runtime is healthy (t_f05dab8d crashed x2 this way). ``hermes_bootstrap.harden_import_path``
cannot help: it runs after the shadowed ``hermes_cli.main`` was already imported.

Fix, using only built-ins:

* ``-P`` (Python >= 3.11, the project floor; also on Windows): the interpreter does not
  prepend the cwd for ``-m``. Passed as an interpreter flag on this one argv, NOT as
  ``PYTHONSAFEPATH`` in the env, so the worker's own children (``python script.py`` in a
  user project) keep normal script-dir semantics. ``venv_sync.relaunch_command`` and
  ``_early_recovery`` both carry interpreter options across a relaunch.
* ``cron.scheduler_worker_env.pin_hermes_tree_on_pythonpath``: the same pin cron's external
  worker and upstream's ``_propagate_module_import_root`` (#122299/#122487, called just before
  the anchor) use, so without the cwd entry the worker still imports exactly the
  dispatcher's tree. Idempotent: re-applied here only so the root stays first after the
  filter below, and so the guarantee does not depend on that upstream call staying put.
* Empty / relative PYTHONPATH entries are dropped: Python resolves them against the cwd,
  which would reopen the same hole.

The process cwd, ``TERMINAL_CWD`` and ``HERMES_KANBAN_WORKSPACE`` are untouched, so tools and
AGENTS.md / context loading stay rooted in the workspace. A resolved shim
(``HERMES_BIN`` path or PATH ``hermes``) is left alone: a console script puts its own
directory on ``sys.path``, not the cwd, so it already owns its imports.

Called from exactly one site: the ``worker-import-isolation`` FORK ANCHOR in
``hermes_cli.kanban_db_dispatch._default_spawn``, right after upstream's
``_propagate_module_import_root`` and before the restart-safe systemd wrapper.
"""

from __future__ import annotations

import os
from pathlib import Path

_MODULE_FORM = ["-m", "hermes_cli.main"]


def isolate_worker_imports(cmd: list[str], env: dict[str, str]) -> list[str]:
    """Return the worker argv with cwd-free imports; mutates ``env`` for module-form launches only."""
    if cmd[1:3] != _MODULE_FORM:
        return cmd
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath

    entries = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e and os.path.isabs(e)]
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
    else:
        env.pop("PYTHONPATH", None)
    pin_hermes_tree_on_pythonpath(env, Path(__file__).resolve().parents[2])
    return [cmd[0], "-P", *cmd[1:]]
