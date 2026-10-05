"""Upstream-candidate fix for #121042: mcp 2.x's ``ToolAnnotations`` model exposes only the
snake_case ``read_only_hint`` field (no ``readOnlyHint`` alias on attribute access), so
``tools.mcp_tool_registration._annotation_read_only_hint`` — written against the 1.x camelCase
name — read ``None`` for every real tool and classed it write-capable. Every MCP call on a
``trust: untrusted`` server then hit the approval gate, reads included. Drop this module and its
call-site anchor (``tools/mcp_tool_registration.py``) once an upstream fix merges (prior art:
#123496, #117840, #111270, #94891).
"""

from typing import Any

from tools.mcp_tool_common import mcp_field


def read_only_hint(annotations: Any) -> bool:
    """True only when *annotations* — an SDK ``ToolAnnotations`` object or a schema-cache
    dict — carries ``read_only_hint``/``readOnlyHint`` exactly ``True``. Missing annotations,
    an explicit ``False``, and non-bool hints that survive in cache dicts are write-capable.
    The MCP SDK coerces some malformed wire values (e.g. ``"yes"``) to bool before this
    hook sees a Tool; this helper cannot reject those already-coerced values.
    """
    if isinstance(annotations, dict):
        hint = annotations.get("read_only_hint", annotations.get("readOnlyHint"))
    else:
        hint = mcp_field(annotations, "read_only_hint", "readOnlyHint")
    return hint is True
