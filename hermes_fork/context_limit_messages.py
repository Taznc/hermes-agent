"""Actionable wording for the ``@`` context-injection hard refusal and soft warning.

T1 module for T2 anchor ``context-limit-copy`` in ``agent/context_references.py``. The
upstream messages at lines 232/236 there say WHAT happened but not why, how the limit
relates to the model's context window, whether anything was applied, or what to do next.
``with_actionable_limit_copy`` wraps ``preprocess_context_references_async`` and rewrites
only the last warning (the hard/soft limit line always appends last), matched against the
pinned upstream regex so an upstream wording change fails safe (returns the result
untouched) instead of silently going inert. Card: t_8925e6d3; spec: t_619998f7.

Pure module: no I/O, no config, no logging, no import of ``agent.context_references`` at
module level (that import lives in the T2 anchor, to avoid a circular import).
"""

from __future__ import annotations

import functools
import re
from typing import Any, Awaitable, Callable

# Pinned upstream wording (agent/context_references.py:232/236, unchanged since 2026-03-21).
# A fullmatch failure means upstream reworded the message; the wrapper then passes the
# result through untouched so this anchor cannot silently drift out of sync (spec N4).
_UPSTREAM_LIMIT_RE = re.compile(
    r"^@ context injection (refused|warning): (\d+) tokens exceeds the (\d+)% (hard|soft) limit \((\d+)\)\.$"
)

_NEXT_STEP = (
    "Reference fewer or smaller files, use a line range such as @file:path:1-200, "
    "or name the file without @ so Hermes reads only what it needs with its tools."
)


def _actionable_refused(tokens: int, limit: int, pct: int, window: int) -> str:
    return (
        f"@ context injection refused: the @ references in this message add up to "
        f"about {tokens:,} tokens, over the {limit:,}-token limit. Hermes caps attached "
        f"@ content at {pct}% of this model's {window:,}-token context window so there "
        f"is room left for the conversation and the reply. Nothing was attached, and "
        f"this message was not sent to the model. {_NEXT_STEP}"
    )


def _actionable_warning(tokens: int, limit: int, pct: int, window: int) -> str:
    return (
        f"@ context injection warning: the @ references in this message add up to "
        f"about {tokens:,} tokens, over the {limit:,}-token guideline ({pct}% of this "
        f"model's {window:,}-token context window). The expanded content is included, "
        f"and this warning does not block submission, but it leaves less room for the "
        f"conversation and may make Hermes compress it sooner. If you don't need whole "
        f"files, use a line range such as @file:path:1-200 or fewer references next time."
    )


def with_actionable_limit_copy(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap ``preprocess_context_references_async`` to rewrite only its last limit warning."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if not result.warnings:
            return result

        old_warning = result.warnings[-1]
        match = _UPSTREAM_LIMIT_RE.fullmatch(old_warning)
        if not match:
            return result

        kind, tokens_s, pct_s, _hard_or_soft, limit_s = match.groups()
        window = kwargs.get("context_length")
        if not isinstance(window, int) or window <= 0:
            return result

        tokens, pct, limit = int(tokens_s), int(pct_s), int(limit_s)
        if kind == "refused":
            new_warning = _actionable_refused(tokens, limit, pct, window)
        else:
            new_warning = _actionable_warning(tokens, limit, pct, window)

        result.warnings[-1] = new_warning

        # H (refused) returns before upstream rebuilds result.message (line 234), so
        # result.message is still the original text there: nothing to rewrite.
        # S (warning) proceeds to expansion, so upstream appends "- " + old_warning
        # after the GENERATED "\n\n--- Context Warnings ---\n" marker (line 243). That
        # marker is only ever generated once, appended after the (possibly user-typed)
        # original message, so the LAST occurrence of the marker string in the final
        # message is always the generated one — a user who types the same marker/old
        # warning text earlier in their message leaves an occurrence that sorts before
        # it. rpartition (not partition) targets that trailing generated block only,
        # so user-authored text is never touched.
        if kind == "warning" and result.expanded and result.message:
            marker = "\n\n--- Context Warnings ---\n"
            head, sep, tail = result.message.rpartition(marker)
            if sep:
                old_line = f"- {old_warning}"
                new_tail = tail.replace(old_line, f"- {new_warning}", 1)
                if new_tail != tail:
                    result.message = head + sep + new_tail

        return result

    return wrapper
