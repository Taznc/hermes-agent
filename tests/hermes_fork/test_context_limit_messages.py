"""Tests for ``hermes_fork.context_limit_messages`` (T1) and its T2 anchor in
``agent/context_references.py``. Spec: t_619998f7 §5 (N1-N6); card: t_8925e6d3.

Uses the real ``agent.context_references`` module with tmp files and no mocks, per spec.
Each test was proven RED by temporarily removing the T2 anchor before writing the fix.
"""

from __future__ import annotations

import asyncio
import inspect
import re
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

    # No refs at all -> no warnings -> the wrapper's early return leaves warnings empty.
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
    only the generated block that follows the user text is rewritten."""
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


_MARKER = "\n\n--- Context Warnings ---\n"
_ATTACHED = "\n\n--- Attached Context ---\n\n"


def _assert_only_generated_block_rewritten(tmp_path: Path, user_text: str, body: str) -> None:
    upstream_fn = ctx_mod.preprocess_context_references_async.__wrapped__
    upstream = asyncio.run(upstream_fn(user_text, cwd=tmp_path, context_length=1_000))
    wrapped = ctx_mod.preprocess_context_references(user_text, cwd=tmp_path, context_length=1_000)

    assert wrapped.expanded and not wrapped.blocked
    new_warning = wrapped.warnings[-1]
    assert new_warning.startswith("@ context injection warning: the @ references")

    # The Attached Context region holding the file body is byte-identical to upstream's.
    assert body in upstream.message
    attached_at = wrapped.message.index(_ATTACHED)
    assert wrapped.message[attached_at:] == upstream.message[upstream.message.index(_ATTACHED):]
    assert body in wrapped.message[attached_at:]

    # The generated block, directly after the user text, carries only the new copy.
    assert wrapped.message[len(user_text.strip()):attached_at] == _MARKER + "- " + new_warning


def test_attached_file_containing_marker_is_untouched(tmp_path: Path):
    """An attached file whose body contains the generated marker must not hijack the
    rewrite (review t_f1978d20 failure mode a): the file body stays byte-identical and
    the generated block shows the new copy."""
    body = "notes" + _MARKER + "- some unrelated bullet\n" + "x" * 1200 + "\n"
    (tmp_path / "only.txt").write_text(body, encoding="utf-8")

    _assert_only_generated_block_rewritten(tmp_path, "Inspect @file:only.txt", body)


def test_attached_file_containing_marker_and_exact_old_line_is_untouched(tmp_path: Path):
    """Failure mode b: the attached body holds the marker AND the exact old warning line
    upstream generates for this very request; the body must not be rewritten."""
    upstream_fn = ctx_mod.preprocess_context_references_async.__wrapped__
    user_text = "Inspect @file:only.txt"

    def body_for(tokens: int) -> str:
        line = f"- @ context injection warning: {tokens} tokens exceeds the 25% soft limit (250)."
        return "notes" + _MARKER + line + "\n" + "x" * 1200 + "\n"

    # A fixed-width (3-digit) count keeps the body length, hence the token estimate, constant.
    (tmp_path / "only.txt").write_text(body_for(999), encoding="utf-8")
    tokens = asyncio.run(upstream_fn(user_text, cwd=tmp_path, context_length=1_000)).injected_tokens
    assert 100 <= tokens <= 999
    body = body_for(tokens)
    (tmp_path / "only.txt").write_text(body, encoding="utf-8")
    upstream = asyncio.run(upstream_fn(user_text, cwd=tmp_path, context_length=1_000))
    assert "- " + upstream.warnings[-1] in body  # the file really holds the exact old line

    _assert_only_generated_block_rewritten(tmp_path, user_text, body)


def test_user_text_surrounding_whitespace_keeps_offset_exact(tmp_path: Path):
    """Upstream strips the final message, removing the user text's leading whitespace
    but keeping trailing whitespace; the rewrite must still land on the generated block."""
    (tmp_path / "only.txt").write_text("x" * 1200, encoding="utf-8")
    user_text = "  \n Inspect @file:only.txt \n\n"
    result = ctx_mod.preprocess_context_references(user_text, cwd=tmp_path, context_length=1_000)

    assert result.warnings[-1].startswith("@ context injection warning: the @ references")
    start = len(user_text.lstrip())
    assert result.message[:start] == user_text.lstrip()
    assert result.message.startswith(_MARKER + "- " + result.warnings[-1], start)
    assert "soft limit" not in result.message
