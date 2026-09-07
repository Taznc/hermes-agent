"""Read-only Desktop JSON-RPC entry point for draft model advice."""

from tui_gateway.method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@method("model_recommendation.get")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from hermes_fork.model_recommendation.service import recommend

        result = recommend(
            draft=params.get("draft"),
            attachments=params.get("attachments", []),
            policy=params.get("policy", "balanced"),
        )
    except ValueError as exc:
        return _err(rid, 4000, str(exc))
    except Exception:
        return _err(rid, 5093, "Model recommendation is temporarily unavailable")
    return _ok(rid, result)


def register(server) -> None:
    _registry.install(server)
