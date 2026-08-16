"""Tests for ``agent.background_review.collect_background_review_actions``.

Companion coverage to ``summarize_background_review_actions`` (see
``test_background_review.py``'s memory_notifications block and
``test_background_review_summary.py``): this function returns the
STRUCTURED per-call records a UI can render as individually expandable
mutations, rather than one flattened summary line. Introduced for
ROADMAP.md Phase 1 (Desktop transcript auditability).
"""

from __future__ import annotations

import json as _json

from agent.background_review import collect_background_review_actions


def _assistant_call(tool_call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": tool_call_id,
                "function": {"name": name, "arguments": _json.dumps(arguments)},
            }
        ],
    }


def _tool_result(tool_call_id: str, payload: dict) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": _json.dumps(payload)}


def test_notifications_off_returns_no_records():
    review_messages = [
        _assistant_call("c1", "memory", {"action": "add", "target": "memory", "content": "x"}),
        _tool_result("c1", {"success": True, "message": "Entry added.", "target": "memory"}),
    ]
    assert collect_background_review_actions(review_messages, [], notification_mode="off") == []


def test_single_memory_add_record():
    review_messages = [
        _assistant_call(
            "c1", "memory", {"action": "add", "target": "memory", "content": "User prefers terse replies"}
        ),
        _tool_result("c1", {"success": True, "message": "Entry added.", "target": "memory"}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records) == 1
    record = records[0]
    assert record["target"] == "memory"
    assert record["label"] == "Memory"
    assert record["operation"] == "add"
    assert record["success"] is True
    assert record["content_preview"] == "User prefers terse replies"


def test_batch_operations_yield_one_record_per_sub_operation():
    """A single ``memory`` call with an ``operations`` batch (add + replace +
    remove) must expand into ONE record per sub-operation, not one record
    for the whole call — the whole point of the expandable detail view is
    seeing each individual mutation."""
    review_messages = [
        _assistant_call(
            "c1",
            "memory",
            {
                "operations": [
                    {"action": "add", "content": "New fact A"},
                    {"action": "replace", "old_text": "stale", "content": "New fact B"},
                    {"action": "remove", "old_text": "obsolete fact"},
                ]
            },
        ),
        _tool_result("c1", {"success": True, "message": "Batch applied.", "target": "memory"}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records) == 3
    ops = [r["operation"] for r in records]
    assert ops == ["add", "replace", "remove"]
    assert records[0]["content_preview"] == "New fact A"
    assert records[1]["content_preview"] == "New fact B"
    assert records[2]["old_preview"] == "obsolete fact"
    assert all(r["success"] is True for r in records)


def test_skill_patch_record_includes_diff_previews():
    review_messages = [
        _assistant_call(
            "c1",
            "skill_manage",
            {"action": "patch", "name": "demo-skill", "old_string": "old approach", "new_string": "new approach"},
        ),
        _tool_result(
            "c1",
            {
                "success": True,
                "message": "Patched SKILL.md in skill 'demo-skill' (1 replacement).",
                "_change": {"old": "old approach", "new": "new approach"},
            },
        ),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records) == 1
    record = records[0]
    assert record["target"] == "skill"
    assert record["label"] == "Skill"
    assert record["operation"] == "patch"
    assert record["skill_name"] == "demo-skill"
    assert record["old_preview"] == "old approach"
    assert record["new_preview"] == "new approach"
    assert record["success"] is True


def test_failed_write_is_included_not_dropped():
    """A memory write that fails (e.g. exceeds the char budget) must still
    surface as a record — with success=False and the tool's error message —
    so the expandable view can show attempted-but-failed operations,
    matching ROADMAP.md Phase 1's 'failed/over-budget attempted-operation
    handling' requirement."""
    review_messages = [
        _assistant_call(
            "c1", "memory", {"action": "add", "target": "memory", "content": "way too long entry"}
        ),
        _tool_result(
            "c1",
            {
                "success": False,
                "error": "Adding this entry would exceed the memory char budget.",
            },
        ),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records) == 1
    record = records[0]
    assert record["success"] is False
    assert "char budget" in record["message"]


def test_user_profile_target_labeled_distinctly():
    review_messages = [
        _assistant_call("c1", "memory", {"action": "add", "target": "user", "content": "Name is Josh"}),
        _tool_result("c1", {"success": True, "message": "Entry added.", "target": "user"}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert records[0]["target"] == "user"
    assert records[0]["label"] == "User profile"


def test_prior_snapshot_tool_call_ids_are_skipped():
    """Mirrors the #14944 de-dup guard in summarize_background_review_actions:
    a tool result already present in the prior conversation snapshot must
    not be re-surfaced as if it just happened in this review pass."""
    prior_tool_msg = _tool_result("c-old", {"success": True, "message": "Entry added.", "target": "memory"})
    review_messages = [
        prior_tool_msg,
        _assistant_call("c-old", "memory", {"action": "add", "target": "memory", "content": "stale"}),
        _assistant_call("c-new", "memory", {"action": "add", "target": "memory", "content": "fresh fact"}),
        _tool_result("c-new", {"success": True, "message": "Entry added.", "target": "memory"}),
    ]

    records = collect_background_review_actions(review_messages, [prior_tool_msg], notification_mode="on")

    assert len(records) == 1
    assert records[0]["content_preview"] == "fresh fact"


def test_content_preview_truncated_at_120_chars():
    long_content = "x" * 200
    review_messages = [
        _assistant_call("c1", "memory", {"action": "add", "target": "memory", "content": long_content}),
        _tool_result("c1", {"success": True, "message": "Entry added.", "target": "memory"}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records[0]["content_preview"]) == 121  # 120 chars + ellipsis
    assert records[0]["content_preview"].endswith("…")


def test_malformed_tool_response_does_not_raise():
    """A non-dict JSON payload (e.g. a bare list from a legacy/wrapper MCP
    response, #59437) must be handled defensively rather than raising."""
    review_messages = [
        _assistant_call("c1", "memory", {"action": "add", "target": "memory", "content": "x"}),
        {"role": "tool", "tool_call_id": "c1", "content": _json.dumps([{"success": True}])},
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    # Malformed (non-dict) payload normalizes to an empty dict -> success=False.
    assert len(records) == 1
    assert records[0]["success"] is False


def test_empty_inputs_return_empty_list():
    assert collect_background_review_actions([], [], notification_mode="on") == []
    assert collect_background_review_actions(None, None, notification_mode="on") == []
