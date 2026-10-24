"""Boot a borrowed data root on the owning root's committed dependency environment.

A launch whose ``HERMES_HOME`` only borrows a checkout -- a test harness's temporary home, a
per-task home, a CI home, often with ``HOME`` repointed as well -- holds no dependency state of
its own for it. The checkout's launcher already execs the owner's store Python, so upstream's
``activate_dependencies`` reaches ``_require_own_dependencies`` with a bare interpreter, and the
first third-party import fails (``ModuleNotFoundError: ruamel``). This selects the owner's
committed environment instead; runtime state (config, cron jobs, sessions) stays in the borrowed
home because nothing here touches ``HERMES_HOME``.

Read-only towards the owner: no owner lock is taken and no owner publication is recovered, so
the selection is re-read after leasing exactly as an unlocked boot does
(``pm.environments.activate_dependencies``). A borrower that synced an environment of its own
(``hermes_cli.venv_sync._prepare_borrowed_launch``) never reaches this: it has a committed
selection. Called from the ``borrowed-home-deps`` anchor in ``pm/environments.py`` before any
third-party import, so stdlib and ``pm``/``hermes_cli`` boot-safe modules only.
"""
from __future__ import annotations

import os
from pathlib import Path


def owner_root(project_root: Path) -> Path | None:
    """The data root whose committed state this borrowing launch should use, else ``None``.

    ``owning_home_root`` asks the root the checkout sits in and the platform default. A release
    tree (``~/.hermes-releases/<sha>``) sits in neither, and a harness that repoints ``HOME``
    moves the platform default too, so the root whose ``tools/`` holds the interpreter the
    checkout's own launcher execs is asked as well -- only the owner publishes that launcher.
    """
    from pm import environments as env

    root = Path(project_root).resolve()
    owner = env.owning_home_root(root)
    if owner is not None:
        return owner
    from hermes_cli._launchers import _launcher_python

    key = env.install_key(root)
    names = ("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)
    for name in names:
        python = _launcher_python(root / ".hermes" / "bin" / name)
        if python is None or not python.is_file():
            continue
        for parent in python.parents:
            if parent.name != "tools" or not (parent.parent / "installs" / key / "facts.json").is_file():
                continue
            try:
                if (parent.parent / "installs" / key).resolve() == env.install_state_dir(root).resolve():
                    return None  # the active root is the owner
            except OSError:
                pass
            return parent.parent
    return None


def borrowed_environment(project_root: Path) -> Path | None:
    """The owner's committed environment, leased for this process, else ``None``."""
    from pm import environments as env

    owner = owner_root(project_root)
    if owner is None:
        return None
    from hermes_cli.runtime_state import lease_generation
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    def selected() -> Path | None:
        # The owner's record under the owner's containment check, as the owner's own boot reads it.
        token = set_hermes_home_override(owner)
        try:
            return env.committed_venv(project_root)
        finally:
            reset_hermes_home_override(token)

    environment = selected()
    if environment is None:
        return None
    release = lease_generation(environment)
    while (current := selected()) not in (None, environment):
        release()
        environment, release = current, lease_generation(current)
    return environment


def activate_borrowed_dependencies(project_root: Path) -> bool:
    """Put the owner's committed environment on ``sys.path``; ``False`` when there is none.

    Mirrors the tail of ``pm.environments.activate_dependencies`` for a checkout that is not a
    sealed payload (a borrower never is: a payload carries its own environment).
    """
    import site
    import sys

    from pm import environments as env

    environment = borrowed_environment(project_root)
    if environment is None:
        return False
    root = Path(project_root).resolve()
    selected = env.site_packages(environment)
    if not selected.is_dir():
        raise RuntimeError(f"dependency environment has no site-packages: {selected}")
    sys.path[:] = [entry for entry in sys.path
                   if Path(entry).name not in ("site-packages", "dist-packages") and Path(entry).resolve() != root]
    site.addsitedir(str(selected))
    sys.path[:] = [str(root), str(selected), *[entry for entry in sys.path if Path(entry).resolve() != selected.resolve()]]
    os.environ["PYTHONPATH"] = os.pathsep.join([str(root), str(selected)])
    os.environ.pop("VIRTUAL_ENV", None)
    prefix = [str(path) for path in (root / ".hermes" / "bin", env.venv_bin_dir(environment)) if path.is_dir()]
    if prefix:
        os.environ["PATH"] = os.pathsep.join([*prefix, os.environ.get("PATH", "")])
    return True
