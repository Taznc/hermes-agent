"""Read-only Desktop JSON-RPC entry point for draft model advice."""

from hermes_cli.profiles import normalize_profile_name, profile_exists, validate_profile_name
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tui_gateway.method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method


def requested_profile_home(params: dict, profile_home):
    """Home to bind for an explicit ``profile``, or None for the launch profile.

    The shared ``_profile_scoped`` decorator falls back to the launch profile for
    any name it cannot resolve. That is wrong here: the unsent draft would reach
    whichever router the launch profile configured, so an explicitly supplied
    profile that is invalid, unknown, or deleted is rejected instead. Omitted or
    blank stays compatible with older clients and means the launch profile.
    """
    raw = params.get("profile")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("profile must be a string")
    if not raw.strip():
        return None
    name = normalize_profile_name(raw)
    validate_profile_name(name)
    if not profile_exists(name):
        raise ValueError("the selected profile does not exist")
    return profile_home(name)


@method("model_recommendation.get")
def _(rid, params: dict) -> dict:
    # Rebound onto the server's globals at install: ``_profile_home``/``_ok``/``_err`` are the
    # server's, and this module's own helpers must be imported rather than referenced.
    from hermes_fork.model_recommendation.gateway_method import requested_profile_home

    try:
        home = requested_profile_home(params, _profile_home)
    except ValueError:
        return _err(rid, 4002, "an existing profile is required for model recommendations")
    token = set_hermes_home_override(home) if home is not None else None
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
    finally:
        if token is not None:
            reset_hermes_home_override(token)
    return _ok(rid, result)


def register(server) -> None:
    _registry.install(server)
