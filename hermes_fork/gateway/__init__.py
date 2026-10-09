"""Fork JSON-RPC methods for the TUI/desktop gateway. Implementation and conventions:
:mod:`hermes_fork.gateway.registry` (the anchor imports it directly so ``fork-budget.sh`` can resolve
the seam; this package re-exports the public API)."""

from hermes_fork.gateway.registry import (
    FORK_PREFIX,
    ForkMethod,
    ForkRpcError,
    fork_method,
    register_fork_gateway_methods,
)

__all__ = ["FORK_PREFIX", "ForkMethod", "ForkRpcError", "fork_method", "register_fork_gateway_methods"]
