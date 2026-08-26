"""Read-only provider account-limit JSON-RPC handler for Desktop plugins.

Disk plugins can use the established ``host.request`` gateway door but are
intentionally prevented from addressing arbitrary REST routes.  This handler
therefore exposes the same sanitized payload as ``/api/account-limits`` without
putting OAuth/Codex credentials in the renderer.
"""

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@method("account_limits.get")
@_profile_scoped
def _(rid, params: dict) -> dict:
    raw_providers = params.get("providers")
    if raw_providers is None:
        providers = None
    elif not isinstance(raw_providers, (list, tuple)) or not all(
        isinstance(provider, str) and provider.strip() for provider in raw_providers
    ):
        return _err(rid, 4000, "providers must be a list of supported provider names")
    else:
        providers = tuple(provider.strip().lower() for provider in raw_providers)

    try:
        from agent.account_usage import fetch_account_limits, serialize_account_usage

        snapshots = fetch_account_limits(providers)
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    except Exception:
        # The account fetcher is fail-open; this is only a final guard against a
        # programming/runtime failure escaping the plugin read surface.
        return _err(rid, 5092, "Account limits are temporarily unavailable")
    return _ok(rid, {"accounts": [serialize_account_usage(snapshot) for snapshot in snapshots]})


def register(server) -> None:
    _registry.install(server)
