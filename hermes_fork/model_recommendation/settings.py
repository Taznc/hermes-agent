"""The composer preset is a setting, separate from auxiliary router credentials."""

from hermes_cli import managed_scope
from hermes_cli.config import _CONFIG_LOCK, is_managed, require_readable_config_before_write
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists, validate_profile_name
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_fork.model_recommendation.service import POLICIES
from utils import _roundtrip_load, atomic_roundtrip_yaml_update

KEY = "model_recommendation.preset"


def _selected_profile(params: dict):
    # The shared config RPC's profile decorator falls back to the launch home
    # for unknown profiles. A persistent composer setting must never do that.
    raw = params.get("profile")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("an explicit profile is required for the recommendation preset")
    name = normalize_profile_name(raw)
    validate_profile_name(name)
    if not profile_exists(name):
        raise ValueError("the selected profile does not exist")
    return name, get_profile_dir(name)


def _profile_type_guard(server, handler):
    def guarded(rid, params):
        # Run before the shared profile decorator, which assumes a string.
        # Every other config key retains its existing handler unchanged.
        if params.get("key") == KEY and not isinstance(params.get("profile"), str):
            return server._err(rid, 4002, "an explicit profile string is required for the recommendation preset")
        return handler(rid, params)
    return guarded


def register(server) -> None:
    def get_preset(params: dict) -> dict:
        name, home = _selected_profile(params)
        token = set_hermes_home_override(home)
        try:
            # Guard the gateway's fail-open reader: corrupt/unreadable YAML is
            # not evidence that the user selected the default preset.
            require_readable_config_before_write(home / "config.yaml")
            section = server._load_cfg().get("model_recommendation")
            value = section.get("preset") if isinstance(section, dict) else None
            if not isinstance(value, str) or value not in POLICIES:
                value = DEFAULT_CONFIG["model_recommendation"]["preset"]
            return {"value": value, "profile": name}
        except Exception:
            raise RuntimeError("Could not read model recommendation preset") from None
        finally:
            reset_hermes_home_override(token)

    def set_preset(rid, params, key, value, session):
        if not isinstance(value, str) or value not in POLICIES:
            return server._err(rid, 4002, "preset must be balanced, save_codex, or best_quality")
        try:
            name, home = _selected_profile(params)
        except ValueError as exc:
            return server._err(rid, 4002, str(exc))
        try:
            with _CONFIG_LOCK:
                if is_managed() or managed_scope.is_key_managed(KEY) or managed_scope.is_key_managed("model_recommendation"):
                    raise ValueError("model recommendation preset is managed")
                path = home / "config.yaml"
                raw = require_readable_config_before_write(path)
                # The atomic updater prefers literal dotted keys over nesting.
                if KEY in raw:
                    raise ValueError("literal model_recommendation.preset key prevents a nested preset write")
                if "model_recommendation" in raw and not isinstance(raw["model_recommendation"], dict):
                    raise ValueError("model_recommendation must be a mapping before writing a preset")
                # Inspect the writer's graph: PyYAML flattens merge references.
                # Even an unanchored target inherited from a root merge is shared.
                _, document = _roundtrip_load(path)
                assert document is not None  # _roundtrip_load returns a mapping even for missing files.
                target = document.get("model_recommendation")
                if target is not None and (
                    target.anchor.value is not None
                    or "model_recommendation" not in dict(document.non_merged_items())
                ):
                    raise ValueError("anchored or merged model_recommendation mapping prevents an isolated preset write")
                # Fresh raw, single-key round trip: never persist defaults,
                # expanded secrets, managed values, or a stale whole-file cache.
                atomic_roundtrip_yaml_update(path, KEY, value)
        except Exception:
            return server._err(rid, 5001, "Could not save model recommendation preset")
        return server._ok(rid, {"key": KEY, "value": value, "profile": name})

    server._CONFIG_GETTERS[KEY] = get_preset
    server._CONFIG_GET_ERR[KEY] = 5001
    server._CONFIG_SETTERS[KEY] = set_preset
    for method in ("config.get", "config.set"):
        server._methods[method] = _profile_type_guard(server, server._methods[method])
