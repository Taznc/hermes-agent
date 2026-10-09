"""Fork JSON-RPC methods for the TUI/desktop gateway (``gateway-fork-methods`` anchor).

``tui_gateway/server.py`` calls :func:`register_fork_gateway_methods` from its single FORK ANCHOR, after
every upstream split module has registered.

A fork method lives in ``hermes_fork/gateway/methods_<area>.py``, is named ``fork.<area>.<name>`` (the
``fork.`` prefix keeps it out of upstream's namespace forever) and declares its own wire contract, so the
upstream catalog test (``tests/tui_gateway/contracts/test_generated.py``) covers it with no edit to
upstream::

    @fork_method("fork.example.echo", params=EchoParams, result=EchoResult, doc="...")
    def _(params: dict) -> dict:
        return {"text": params.get("text", "")}

and is listed in :func:`_fork_method_modules`. The list is a static import on purpose:
``scripts/fork-budget.sh`` proves a fork module is live by following import statements from the anchor,
so a module reached only through ``pkgutil`` would be reported ORPHANED. A ``methods_*.py`` file missing
from the list fails ``tests/hermes_fork/gateway`` instead of silently never registering.

Handlers take the params dict and return the result dict; a domain failure raises :class:`ForkRpcError`.
Contracts are declared at REGISTRATION time, not import time, so the contract generator (which imports
only ``tui_gateway.contracts``) keeps rendering upstream's committed TypeScript/OpenRPC artefacts
byte-for-byte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, Iterable

from tui_gateway.contracts import registry as _contracts
from tui_gateway.contracts.base import Params, Result

logger = logging.getLogger(__name__)

FORK_PREFIX = "fork."


class ForkRpcError(Exception):
    """A handler's domain error, answered as a JSON-RPC error frame."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ForkMethod:
    name: str
    handler: Callable[[dict], dict]
    params: type[Params]
    result: type[Result]
    doc: str


# Declarations by method name. A re-import of the SAME module (tests re-import the server under
# ``patch.dict(sys.modules)``) replaces its entry; a second module claiming the name is a bug.
_DECLARED: dict[str, ForkMethod] = {}


def fork_method(name: str, *, params: type[Params], result: type[Result], doc: str = ""):
    if not name.startswith(FORK_PREFIX) or len(name) == len(FORK_PREFIX):
        raise ValueError(f"fork gateway method {name!r} must be named '{FORK_PREFIX}<area>.<name>'")

    def dec(fn: Callable[[dict], dict]):
        prior = _DECLARED.get(name)
        if prior is not None and prior.handler.__module__ != fn.__module__:
            raise RuntimeError(f"fork gateway method {name!r} declared by both "
                               f"{prior.handler.__module__} and {fn.__module__}")
        _DECLARED[name] = ForkMethod(name, fn, params, result, doc)
        return fn

    return dec


# Fork contracts outside the ``fork.`` namespace (server requests / events whose wire name the desktop
# already dispatches on, e.g. ``plugin.request``), declared at registration by a fork module.
_FORK_CONTRACT_NAMES: set[str] = set()


def mark_fork_contract(*names: str) -> None:
    """Record fork-declared contract names so :func:`upstream_only` keeps them out of the generated files."""
    _FORK_CONTRACT_NAMES.update(names)


def upstream_only(table: dict) -> dict:
    """*table* (METHODS / SERVER_REQUESTS / EVENTS) without fork contracts (``gateway-contracts-upstream-only``
    anchor in ``scripts/gen_gateway_contracts.py``): the committed upstream TS/OpenRPC artefacts render the
    same bytes whether or not ``tui_gateway.server`` (and so this registry) ran first in the interpreter."""
    return {name: c for name, c in table.items()
            if not name.startswith(FORK_PREFIX) and name not in _FORK_CONTRACT_NAMES}


upstream_methods = upstream_only  # F24 name, kept for callers of the original anchor


def _fork_method_modules() -> tuple[ModuleType, ...]:
    """Every fork gateway method module. Add each new ``methods_*`` module here."""
    from hermes_fork.gateway import methods_compaction, methods_ping, methods_session_archive

    return (methods_ping, methods_session_archive, methods_compaction)


def _default_modules() -> tuple[ModuleType, ...]:
    try:
        return _fork_method_modules()
    except Exception:
        # Same policy as the contract checks: loud under the test suite, logged in production so a
        # broken fork module cannot take the whole gateway down at import.
        if _contracts.STRICT:
            raise
        logger.exception("fork gateway method modules failed to import; no fork.* methods registered")
        return ()


def _install_gateway_seams(server) -> None:
    try:
        from hermes_fork.ui.gateway import install as install_plugin_ui
        install_plugin_ui(server)
    except Exception:
        if _contracts.STRICT:
            raise
        logger.exception("plugin UI bridge failed to install; plugin ui.request will fall back")

    try:
        from hermes_fork.gateway.config_cache import install as install_config_cache
        install_config_cache(server)
    except Exception:
        if _contracts.STRICT:
            raise
        logger.exception("config cache adapter failed to install; retaining upstream raw config handling")


def _rpc_handler(method: ForkMethod):
    def handle(rid, params: dict) -> dict:
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": method.handler(params)}
        except ForkRpcError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": exc.message}}

    handle.__name__ = handle.__qualname__ = f"fork_rpc[{method.name}]"
    return handle


def register_fork_gateway_methods(server, modules: Iterable[ModuleType] | None = None) -> list[str]:
    """Declare the contract of every fork method in *modules* (default: :func:`_fork_method_modules`)
    and register it on *server* through upstream's one registration seam (``server.register_method``).
    Idempotent. Returns the registered names.

    The default call (the anchor) also installs the other fork gateway seams that need the fully
    registered server module: the plugin UI bridge (:func:`hermes_fork.ui.gateway.install`)."""
    if modules is None:
        _install_gateway_seams(server)
    names = {m.__name__ for m in (_default_modules() if modules is None else modules)}
    methods = sorted((m for m in _DECLARED.values() if m.handler.__module__ in names), key=lambda m: m.name)
    for method in methods:
        if method.name not in _contracts.METHODS:
            _contracts.method(method.name, params=method.params, result=method.result, doc=method.doc)
        server.register_method(method.name, _rpc_handler(method))
    return [m.name for m in methods]
