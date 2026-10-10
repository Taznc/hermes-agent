"""mcp 2.x readOnlyHint reading (upstream #121042; fixed upstream in ca398ea39e, fork anchor
``mcp-read-only-hint`` retired at the 2026-10-09 sync). Kept as a regression contract on
``tools.mcp_tool_registration._annotation_read_only_hint``.

Under mcp 2.0.0 ``mcp.types.Tool.annotations`` is a ``ToolAnnotations`` pydantic model whose
only accessible attribute is snake_case ``read_only_hint`` — the SDK does not expose a
``readOnlyHint`` camelCase attribute, so the original check (written for the 1.x shape) always
read ``None`` and classed every tool write-capable. This exercises the real ``mcp.types.Tool``
plus the fallback shapes (schema-cache dict, a bare object without annotations) the registration
path also has to handle.
"""

from __future__ import annotations

from types import SimpleNamespace

import mcp.types as mcp_types

from tools.mcp_tool_registration import _annotation_read_only_hint


def _real_tool(read_only_hint) -> mcp_types.Tool:
    annotations = None if read_only_hint is None else {"readOnlyHint": read_only_hint}
    payload = {"name": "x", "inputSchema": {"type": "object"}}
    if annotations is not None:
        payload["annotations"] = annotations
    return mcp_types.Tool.model_validate(payload)


class TestRealSdkToolAnnotations:
    """``mcp.types.Tool`` built the way the 2.x client actually returns it."""

    def test_true_hint_is_read_only(self):
        tool = _real_tool(True)
        assert _annotation_read_only_hint(tool) is True

    def test_missing_annotations_is_write_capable(self):
        tool = _real_tool(None)
        assert _annotation_read_only_hint(tool) is False

    def test_false_hint_is_write_capable(self):
        tool = _real_tool(False)
        assert _annotation_read_only_hint(tool) is False

    def test_sdk_coerces_non_bool_before_hook(self):
        # SDK validation loses the raw type; rejecting malformed wire input is a separate fix.
        tool = _real_tool("yes")
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert _annotation_read_only_hint(tool) is True


class TestCacheDictAnnotations:
    """Schema-cache replay carries annotations as a plain dict on a ``SimpleNamespace``."""

    def test_dict_annotations_true(self):
        tool = SimpleNamespace(annotations={"readOnlyHint": True})
        assert _annotation_read_only_hint(tool) is True

    def test_dict_annotations_false(self):
        tool = SimpleNamespace(annotations={"readOnlyHint": False})
        assert _annotation_read_only_hint(tool) is False

    def test_no_annotations_attribute(self):
        tool = SimpleNamespace()
        assert _annotation_read_only_hint(tool) is False

    def test_non_bool_hint_is_write_capable(self):
        """A server lying with a truthy non-bool value must still fail closed."""
        tool = SimpleNamespace(annotations={"readOnlyHint": "yes"})
        assert _annotation_read_only_hint(tool) is False
