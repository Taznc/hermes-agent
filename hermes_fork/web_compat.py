"""Fork-owned plugin compatibility mappings for the dashboard web server."""

FORK_PLUGIN_COMPAT_LAZY = {
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
