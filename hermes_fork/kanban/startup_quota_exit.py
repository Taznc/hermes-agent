"""Exit code for the non-quiet ``chat -q`` tail, honouring a startup quota wall.

The Kanban dispatcher spawns workers with ``chat -q``. When credential resolution itself
hits a quota wall (e.g. the Anthropic pool benched every key for the worker's model, a
rate-limit-tagged ``AuthError``), ``chat()`` returns before any turn runs, so
``_last_turn_result`` is None. The ``-Q`` path already maps that to
``KANBAN_RATE_LIMIT_EXIT_CODE`` via ``_credentials_rate_limited``; the ``-q`` tail did not
and exited 1, which the dispatcher books as ``crashed``. Two of those trip
``failure_limit`` and give the card up while the quota resets on its own (hermes-next
t_60a5bf65 runs 176/177).

Shim: upstream 25b6a9f010 ("fix(kanban): preserve credential startup failure exit codes")
makes the same fix inline. When the weekly sync brings it in, take upstream's line at the
``startup-quota-exit`` anchor and delete this module and its test.
"""
from __future__ import annotations


def single_query_exit_code(cli) -> int:
    from hermes_cli.cli_single_query import _single_query_exit_code

    return _single_query_exit_code(
        cli._last_turn_result,
        credentials_rate_limited=getattr(cli, "_credentials_rate_limited", False))
