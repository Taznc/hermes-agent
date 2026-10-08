"""Exercise the registry seam, not just direct installation of the adapter."""
from types import SimpleNamespace
import logging

import pytest

from hermes_fork.gateway import config_cache, registry
from hermes_fork.ui import gateway as plugin_ui


def _server():
    return SimpleNamespace(_load_cfg_raw=lambda: {}, _save_cfg=lambda cfg: None)


@pytest.mark.parametrize("ui_error", [False, True])
def test_default_registration_installs_cache_even_if_ui_bridge_fails(monkeypatch, ui_error):
    server = _server()
    monkeypatch.setattr(registry._contracts, "STRICT", False)
    monkeypatch.setattr(registry, "_default_modules", lambda: ())

    def ui_install(server):
        if ui_error:
            raise RuntimeError("UI test failure")

    monkeypatch.setattr(plugin_ui, "install", ui_install)
    registry.register_fork_gateway_methods(server)
    assert getattr(server._load_cfg_raw, "_fork_config_cache", False)
    installed = server._load_cfg_raw
    registry.register_fork_gateway_methods(server)
    assert server._load_cfg_raw is installed


@pytest.mark.parametrize("missing", ["_load_cfg_raw", "_save_cfg"])
def test_generic_rpc_host_without_raw_cache_seam_remains_supported(monkeypatch, missing):
    server = _server()
    delattr(server, missing)
    monkeypatch.setattr(registry._contracts, "STRICT", True)
    monkeypatch.setattr(registry, "_default_modules", lambda: ())
    monkeypatch.setattr(plugin_ui, "install", lambda server: None)
    registry.register_fork_gateway_methods(server)
    assert not getattr(getattr(server, "_load_cfg_raw", None), "_fork_config_cache", False)


def test_explicit_module_registration_leaves_cache_untouched():
    server = _server()
    original = server._load_cfg_raw
    registry.register_fork_gateway_methods(server, modules=())
    assert server._load_cfg_raw is original


@pytest.mark.parametrize("strict", [False, True])
def test_install_failure_keeps_original_reader_and_obeys_registry_policy(monkeypatch, caplog, strict):
    server = _server()
    original = server._load_cfg_raw
    monkeypatch.setattr(registry._contracts, "STRICT", strict)
    monkeypatch.setattr(plugin_ui, "install", lambda server: None)

    def fail(server):
        raise RuntimeError("cache test failure")

    monkeypatch.setattr(config_cache, "install", fail)
    if strict:
        with pytest.raises(RuntimeError, match="cache test failure"):
            registry._install_gateway_seams(server)
    else:
        with caplog.at_level(logging.ERROR):
            registry._install_gateway_seams(server)
        assert "config cache adapter failed to install" in caplog.text
    assert server._load_cfg_raw is original
