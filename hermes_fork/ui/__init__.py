"""Plugin UI bridge (roadmap X02, Python half): lets a plugin tool ask the attached UI a question and
wait for the answer, or push a fire-and-forget event, over the TUI/desktop gateway wire.

A plugin tool handler that accepts a ``ui`` kwarg gets a :class:`hermes_fork.ui.bridge.PluginUI`::

    def handle(args, ui=None, clarify_callback=None, **_):
        answer = ui.request("myplugin/pick", {"options": [...]})   # {"payload": ...} | {"unsupported": True} | None
        ui.emit("myplugin/progress", {"pct": 40})                   # True when a frame was written

``ctx.fork.ui`` is the same surface for code that is not a tool handler (hooks), resolved against the
turn that is running when it is called. ``ctx.fork.register_tool(..., interactive=True)`` registers a
tool through the stock ``ctx.register_tool`` and marks it interactive: it never runs in a parallel
batch and its tool.start/complete always reach the UI (the inline card hangs off that tool call).

Wire (declared from :mod:`hermes_fork.ui.contracts` at gateway registration, kept out of the generated
upstream TS/OpenRPC): server request ``plugin.request {kind, payload} -> {payload} | {unsupported: true}``
and event ``plugin.event {kind, payload}``. ``kind`` must be namespaced ``<plugin_id>/<name>``.

Seams (each a FORK ANCHOR in an upstream file; this package imports upstream freely):

- ``hermes_cli/plugins.py``            ``plugin-context-fork``      -> :mod:`hermes_fork.plugin_context`
- ``model_tools.py``                   ``plugin-ui-dispatch``       -> :mod:`hermes_fork.ui.dispatch`
- ``agent/tool_dispatch_helpers.py``   ``interactive-plugin-tools`` -> :mod:`hermes_fork.ui.interactive`
- ``tui_gateway/agent_callbacks.py``   ``plugin-ui-bridge``         -> :mod:`hermes_fork.ui.gateway`
- ``scripts/gen_gateway_contracts.py`` ``gateway-contracts-upstream-only`` (extended)
- ``tui_gateway/server.py``            ``gateway-fork-methods`` (existing; installs the gateway side)

This ``__init__`` stays import-free: ``agent/tool_dispatch_helpers.py`` imports
:mod:`hermes_fork.ui.interactive` very early in agent start-up.
"""
