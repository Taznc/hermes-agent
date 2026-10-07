"""Tests for ``hermes_fork.context_limit_messages`` (T1) and its T2 anchor in
``agent/context_references.py``. Spec: t_619998f7 §5 (N1-N6); card: t_8925e6d3.

Uses the real ``agent.context_references`` module with tmp files and no mocks, per spec.
Each test was proven RED by temporarily removing the T2 anchor before writing the fix.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import agent.context_references as ctx_mod
from hermes_fork.context_limit_messages import _UPSTREAM_LIMIT_RE


def test_hard_refusal_is_actionable(tmp_path: Path):
    """N1: hard refusal (context_references.py:232) gets actionable wording."""
    (tmp_path / "first.txt").write_text("x" * 1200, encoding="utf-8")
    (tmp_path / "second.txt").write_text("x" * 1200, encoding="utf-8")

    result = ctx_mod.preprocess_context_references(
        "Inspect @file:first.txt and @file:second.txt",
        cwd=tmp_path,
        context_length=1_000,
    )

    assert result.blocked
    assert not result.expanded
    last = result.warnings[-1]
    assert last.startswith("@ context injection refused: ")
    import re

    assert re.search(r"about [\d,]+ tokens, over the 500-token limit", last)
    assert "50% of this model's 1,000-token context window" in last
    assert "Nothing was attached, and this message was not sent to the model." in last
    assert "@file:path:1-200" in last
    assert "hard limit" not in last
    assert "exceeds the" not in last


def test_soft_warning_is_actionable_and_expansion_proceeds(tmp_path: Path):
    """N2: soft warning (context_references.py:236) gets actionable wording;
    expansion proceeds and the new sentence also appears in result.message."""
    (tmp_path / "only.txt").write_text("x" * 1200, encoding="utf-8")

    result = ctx_mod.preprocess_context_references(
        "Inspect @file:only.txt",
        cwd=tmp_path,
        context_length=1_000,
    )

    assert not result.blocked
    assert result.expanded
    assert "--- Attached Context ---" in result.message

    last = result.warnings[-1]
    assert last.startswith("@ context injection warning: ")
    assert "250-token guideline (25% of this model's 1,000-token context window)" in last
    assert "The expanded content is included" in last
    assert "this warning does not block submission" in last
    assert "the message was sent" not in last.lower()

    assert last in result.message
    marker = "\n\n--- Context Warnings ---\n"
    assert marker in result.message
    assert "soft limit" not in result.warnings[0] if result.warnings else True
    assert "soft limit" not in "\n".join(result.warnings)
    assert "soft limit" not in result.message


def test_other_warnings_untouched(tmp_path: Path):
    """N3: the max-refs-per-message warning (line 222) is byte-equal to upstream's,
    unaffected by this wrapper."""
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("data", encoding="utf-8")
    message = " ".join(f"@file:f{i}.txt" for i in range(20))

    wrapped_result = asyncio.run(
        ctx_mod.preprocess_context_references_async(message, cwd=tmp_path, context_length=10_000_000)
    )
    upstream_fn = ctx_mod.preprocess_context_references_async.__wrapped__
    upstream_result = asyncio.run(upstream_fn(message, cwd=tmp_path, context_length=10_000_000))

    assert wrapped_result.warnings == upstream_result.warnings


def test_upstream_wording_contract(tmp_path: Path):
    """N4 (retirement tripwire): the pinned upstream regex still matches the raw
    __wrapped__ output. If upstream reworks the message this test goes red first,
    before the anchor silently goes inert."""
    upstream_fn = ctx_mod.preprocess_context_references_async.__wrapped__

    (tmp_path / "first.txt").write_text("x" * 1200, encoding="utf-8")
    (tmp_path / "second.txt").write_text("x" * 1200, encoding="utf-8")
    hard_result = asyncio.run(
        upstream_fn(
            "Inspect @file:first.txt and @file:second.txt", cwd=tmp_path, context_length=1_000,
        )
    )
    assert _UPSTREAM_LIMIT_RE.fullmatch(hard_result.warnings[-1])

    (tmp_path / "only.txt").write_text("x" * 1200, encoding="utf-8")
    soft_result = asyncio.run(
        upstream_fn("Inspect @file:only.txt", cwd=tmp_path, context_length=1_000)
    )
    assert _UPSTREAM_LIMIT_RE.fullmatch(soft_result.warnings[-1])


def test_sync_entrypoint_gets_new_copy(tmp_path: Path):
    """N5: the sync CLI/TUI entrypoint (preprocess_context_references) also gets the
    new copy (it resolves the module global at call time, per the spec)."""
    (tmp_path / "first.txt").write_text("x" * 1200, encoding="utf-8")
    (tmp_path / "second.txt").write_text("x" * 1200, encoding="utf-8")

    result = ctx_mod.preprocess_context_references(
        "Inspect @file:first.txt and @file:second.txt",
        cwd=tmp_path,
        context_length=1_000,
    )

    last = result.warnings[-1]
    assert last.startswith("@ context injection refused: ")
    assert "Nothing was attached" in last


def test_wrapper_contract(tmp_path: Path):
    """N6: wrapper identity, fail-safe pass-through on no match or missing window."""
    assert inspect.iscoroutinefunction(ctx_mod.preprocess_context_references_async)
    assert ctx_mod.preprocess_context_references_async.__wrapped__ is not None

    # Unmatched last warning (no refs at all -> no warnings -> early return) returns
    # the same object unchanged.
    result = asyncio.run(
        ctx_mod.preprocess_context_references_async("no refs here", cwd=tmp_path, context_length=1_000)
    )
    assert result.warnings == []

    # context_length=0 fails the `window > 0` guard -> pass through untouched.
    (tmp_path / "first.txt").write_text("x" * 1200, encoding="utf-8")
    (tmp_path / "second.txt").write_text("x" * 1200, encoding="utf-8")
    zero_window_result = asyncio.run(
        ctx_mod.preprocess_context_references_async(
            "Inspect @file:first.txt and @file:second.txt", cwd=tmp_path, context_length=0,
        )
    )
    last = zero_window_result.warnings[-1]
    assert "hard limit" in last
    assert "context injection refused: the @ references" not in last


def test_user_typed_marker_and_old_warning_text_preserved(tmp_path: Path):
    """Regression: a user message that happens to contain the literal generated
    marker plus upstream old-style warning text must be preserved byte-for-byte;
    only the GENERATED trailing block (after the last marker) is rewritten."""
    (tmp_path / "only.txt").write_text("x" * 1200, encoding="utf-8")

    fake_old_warning = "@ context injection warning: 60000 tokens exceeds the 25% soft limit (50000)."
    user_text = (
        "Please note\n\n--- Context Warnings ---\n- " + fake_old_warning
        + "\nInspect @file:only.txt"
    )

    result = ctx_mod.preprocess_context_references(user_text, cwd=tmp_path, context_length=1_000)

    assert result.expanded
    # The user-typed marker + fake warning line, verbatim, must still be present.
    assert ("--- Context Warnings ---\n- " + fake_old_warning) in result.message
    # The real (generated, trailing) warning must be the new actionable copy.
    assert result.warnings[-1].startswith("@ context injection warning: the @ references")
    assert result.warnings[-1] in result.message
