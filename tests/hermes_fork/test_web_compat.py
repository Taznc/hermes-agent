"""Fork compatibility mappings kept outside the upstream-sorted lazy registry."""

import importlib
import warnings

EXPECTED_FORK_PLUGIN_COMPAT_LAZY = {
    "ChatFileUpload": ("hermes_cli.web_models", "ChatFileUpload"),
    "fs_desktop_plugins_root": (
        "hermes_cli.web_routers.files",
        "fs_desktop_plugins_root",
    ),
    "fs_agent_plugins_root": ("hermes_cli.web_routers.files", "fs_agent_plugins_root"),
    "fs_read_plugin_source": ("hermes_cli.web_routers.files", "fs_read_plugin_source"),
    "get_account_limits": ("hermes_fork.account_limits.routes", "get_account_limits"),
    "upload_chat_file": ("hermes_cli.web_routers.files", "upload_chat_file"),
}


def test_fork_plugin_compat_entries_merge_and_resolve_to_their_targets():
    from hermes_fork.web_compat import FORK_PLUGIN_COMPAT_LAZY
    from hermes_cli import web_server

    assert FORK_PLUGIN_COMPAT_LAZY == EXPECTED_FORK_PLUGIN_COMPAT_LAZY

    for name, target in EXPECTED_FORK_PLUGIN_COMPAT_LAZY.items():
        assert web_server._PLUGIN_COMPAT_LAZY[name] == target
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert getattr(web_server, name) is getattr(importlib.import_module(target[0]), target[1])
