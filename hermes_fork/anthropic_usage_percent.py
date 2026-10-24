"""Anthropic account-usage windows: ``utilization`` is already a 0-100 percent.

``GET https://api.anthropic.com/api/oauth/usage`` reports each window as
``{"utilization": 4.0, "resets_at": ...}`` where 4.0 means 4% (a live payload in the
same response carries ``seven_day: 69.0``). Upstream's ``agent.account_usage`` parses it
with ``fraction=True``, which multiplies any value <= 1 by 100: a session at 1% reads
as 100% and the Desktop usage pill, ``/usage`` and the Kanban weekly guard all see an
exhausted window until usage passes 1%. Anthropic never sends a 0-1 fraction here, so
the scaling is disabled for the ``utilization`` field only. Upstream ``main`` shares the
bug; this fork module wraps the parser at the ``anthropic-utilization-percent`` anchor.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

ANTHROPIC_USED_KEY = "utilization"


def percent_utilization(usage_windows: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``_usage_windows`` so the Anthropic ``utilization`` field is never fraction-scaled."""

    @functools.wraps(usage_windows)
    def wrapper(source, mapping, used_key, reset_key, *, fraction=False, **kwargs):
        if used_key == ANTHROPIC_USED_KEY:
            fraction = False
        return usage_windows(source, mapping, used_key, reset_key, fraction=fraction, **kwargs)

    return wrapper
