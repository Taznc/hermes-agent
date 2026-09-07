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
    assert record["state"] == "completed"
    assert record["reason"] == "memory add completed."
    assert record["change_summary"] == "Before: no new record. After: record added."
    assert "content_preview" not in record


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
    assert records[0]["change_summary"] == "Before: no new record. After: record added."
    assert records[1]["change_summary"] == "Before: prior record. After: updated record."
    assert records[2]["change_summary"] == "Before: existing record. After: record removed."
    assert all(r["success"] is True for r in records)


def test_skill_batch_records_every_operation_without_source_content():
    secret = "sk-live-secret-correct-horse-battery-staple"
    review_messages = [
        _assistant_call(
            "c1",
            "skill_manage",
            {
                "operations": [
                    {"action": "create", "name": "demo-skill", "content": secret},
                    {"action": "patch", "name": "demo-skill", "old_string": secret, "new_string": secret},
                    {"action": "write_file", "name": "other-skill", "file_path": "references/private.md", "file_content": secret},
                ]
            },
        ),
        _tool_result("c1", {"success": True, "message": "Batch applied."}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records) == 3
    assert [record["operation"] for record in records] == ["create", "patch", "write_file"]
    assert [record["skill_name"] for record in records] == ["demo-skill", "demo-skill", "other-skill"]
    assert all(record["target"] == "skill" and record["label"] == "Skill" for record in records)
    assert all(record["success"] is True and record["state"] == "completed" for record in records)
    assert records[1]["change_summary"] == "Before: prior record. After: updated record."
    serialized = _json.dumps(records)
    assert secret not in serialized
    assert not {"content_preview", "old_preview", "new_preview"} & set().union(*(record.keys() for record in records))


def test_legacy_flat_skill_call_remains_renderable():
    review_messages = [
        _assistant_call("c1", "skill_manage", {"action": "patch", "name": "demo-skill", "old_string": "private", "new_string": "private"}),
        _tool_result("c1", {"success": True, "message": "Patched skill."}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert len(records) == 1
    assert records[0]["operation"] == "patch"
    assert records[0]["skill_name"] == "demo-skill"
    assert "private" not in _json.dumps(records)


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
    assert record["state"] == "failed"
    assert record["message"] == "Memory add did not complete."


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
    assert records[0]["state"] == "completed"


def test_adversarial_sensitive_values_are_never_serialized():
    secret = "sk-live-secret-password=correct-horse-battery-staple"
    review_messages = [
        _assistant_call("c1", "memory", {"action": "replace", "target": "user", "content": secret, "old_text": secret}),
        _tool_result("c1", {"success": False, "error": f"Could not save {secret}", "target": "user"}),
    ]

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    serialized = _json.dumps(records)
    assert secret not in serialized
    assert records[0]["target"] == "user"
    assert records[0]["state"] == "failed"
    assert records[0]["change_summary"] == "No stored content was changed."


def test_terminal_outcomes_are_explicit_and_safe():
    review_messages = []
    for index, outcome in enumerate(("no_op", "skipped", "declined", "failed")):
        call_id = f"c{index}"
        review_messages.extend([
            _assistant_call(call_id, "memory", {"action": "replace", "target": "memory", "content": "private"}),
            _tool_result(call_id, {"success": outcome != "failed", "outcome": outcome, "message": "private detail"}),
        ])

    records = collect_background_review_actions(review_messages, [], notification_mode="on")

    assert [record["state"] for record in records] == ["no_op", "skipped", "declined", "failed"]
    assert [record["success"] for record in records] == [False, False, False, False]
    assert all("private" not in _json.dumps(record) for record in records)


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
