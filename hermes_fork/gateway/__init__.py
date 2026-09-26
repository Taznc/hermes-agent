"""Fork JSON-RPC methods for the TUI/desktop gateway.

``tui_gateway/server.py`` calls :func:`register_fork_gateway_methods` from its single
``gateway-fork-methods`` FORK ANCHOR, after every upstream split module has registered.

A fork method lives in ``hermes_fork/gateway/methods_<area>.py``, is named ``fork.<area>.<name>``
(the ``fork.`` prefix keeps it out of upstream's namespace forever) and declares its own wire
contract, so the upstream catalog test (``tests/tui_gateway/contracts/test_generated.py``) covers it
with no edit to upstream::

    @fork_method("fork.example.echo", params=EchoParams, result=EchoResult, doc="...")
    def _(params: dict) -> dict:
        return {"text": params.get("text", "")}

Handlers take the params dict and return the result dict; a domain failure raises
:class:`ForkRpcError`. Contracts are declared at REGISTRATION time, not import time, so the contract
generator (which imports only ``tui_gateway.contracts``) keeps rendering upstream's committed
TypeScript/OpenRPC artefacts byte-for-byte.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from typing import Callable

from tui_gateway.contracts import registry as _contracts
from tui_gateway.contracts.base import Params, Result

logger = logging.getLogger(__name__)

FORK_PREFIX = "fork."
_MODULE_PREFIX = "methods_"


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


def discover_fork_methods(package: str = __name__) -> list[ForkMethod]:
    """Import every ``methods_*`` module of *package*; return the methods they declare."""
    pkg = importlib.import_module(package)
    modules = set()
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.name.startswith(_MODULE_PREFIX):
            continue
        qualname = f"{package}.{info.name}"
        try:
            importlib.import_module(qualname)
        except Exception:
            # Same policy as the contract checks: loud under the test suite, logged in production
            # so one broken fork module cannot take the whole gateway down at import.
            if _contracts.STRICT:
                raise
            logger.exception("fork gateway module %s failed to import; its methods are not registered", qualname)
            continue
        modules.add(qualname)
    return sorted((m for m in _DECLARED.values() if m.handler.__module__ in modules), key=lambda m: m.name)


def _rpc_handler(method: ForkMethod):
    def handle(rid, params: dict) -> dict:
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": method.handler(params)}
        except ForkRpcError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": exc.message}}

    handle.__name__ = handle.__qualname__ = f"fork_rpc[{method.name}]"
    return handle


def register_fork_gateway_methods(server, package: str = __name__) -> list[str]:
    """Declare each discovered fork method's contract and register it on *server* through
    upstream's one registration seam (``server.register_method``). Returns the registered names."""
    methods = discover_fork_methods(package)
    for method in methods:
        if method.name not in _contracts.METHODS:
            _contracts.method(method.name, params=method.params, result=method.result, doc=method.doc)
        server.register_method(method.name, _rpc_handler(method))
    return [m.name for m in methods]


__all__ = ["FORK_PREFIX", "ForkMethod", "ForkRpcError", "discover_fork_methods", "fork_method",
           "register_fork_gateway_methods"]
