#!/usr/bin/env python3
"""Propose handing a new topic off into a brand-new session, as an inline card in the
desktop chat.

Long-lived sessions accumulate a growing cached prefix — cache-read tokens per API call
roughly triple as a session grows past a few hundred tool calls, and a topic pivot inside
one long session compounds it. This tool lets the agent (on its own judgment: a topic
pivot, or the session crossing a length heuristic) propose splitting the new topic into a
fresh session instead of continuing to grow this one.

The card (approve / decline) lives in the desktop renderer, so this tool round-trips
through the gateway's blocking-prompt bridge (the one ``clarify``/``setup_mcp`` use):
tui_gateway emits ``session.propose.request``, the renderer shows an approval card and,
on Approve, creates a brand-new session seeded with ``topic`` as its first message and
switches the user into it. On Decline nothing happens and this session continues.

The new session deliberately does NOT inherit this session's profile, model, or history —
context arrives entirely via the seeded ``topic`` message (decided with Josh, 2026-09-19).

Lives in the ``desktop_ui`` toolset, which the GUI gateway enables only for desktop-sourced
sessions; elsewhere the agent should just keep working in the current session (there is no
terminal equivalent — starting a new session is `/new`, which the user drives themselves).
"""

import json
from typing import Callable, Optional

from tools.registry import registry, tool_error


def propose_new_session_tool(
    topic: str = "", reason: str = "", callback: Optional[Callable] = None,
) -> str:
    """Ask the desktop GUI to offer a fresh-session hand-off; return its JSON outcome."""
    if callback is None:
        return tool_error(
            "propose_new_session is only available in the Hermes desktop app. "
            "Continue in this session, or ask the user to start a new one with /new.")

    topic_text = (topic or "").strip()
    if not topic_text:
        return tool_error("topic is required — the seeded first message for the new session.")

    try:
        raw = callback(topic_text, (reason or "").strip())
    except Exception as exc:
        return tool_error(f"propose_new_session flow failed: {exc}")

    if not raw:
        # The renderer never answered (timeout / closed window). Distinct from an explicit
        # decline, which arrives as {"status": "declined"}.
        return json.dumps({
            "status": "unanswered",
            "note": ("The user did not respond to the new-session card. Do not retry "
                     "immediately; continue in this session."),
        }, ensure_ascii=False)

    # Desktop answers with a JSON object; pass it through, else wrap the raw text.
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "detail": str(raw)}, ensure_ascii=False)


PROPOSE_NEW_SESSION_SCHEMA = {
    "name": "propose_new_session",
    "description": (
        "Propose handing a new topic off into a brand-new, clean session — an inline "
        "approval card; blocks until the user acts. Use this on your own judgment when "
        "the conversation pivots to an unrelated topic, or once this session has run "
        "long (very roughly 100-150 tool calls) and a growing cached prefix is no "
        "longer earning its keep. On Approve, the desktop creates a fresh session "
        "seeded with `topic` as its first message and switches the user into it — it "
        "does NOT inherit this session's profile, model, or history. On Decline or no "
        "response, say nothing about it and keep working here; never re-propose in the "
        "same turn sequence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": (
                    "The seeded first message for the new session — write it as if you "
                    "were the user opening a fresh chat: state the topic and any context "
                    "the new session needs, since it starts with NO memory of this one."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One sentence on the card: why splitting off now helps.",
            },
        },
        "required": ["topic"],
    },
}


registry.register(
    name="propose_new_session",
    toolset="desktop_ui",
    schema=PROPOSE_NEW_SESSION_SCHEMA,
    handler=lambda args, **kw: propose_new_session_tool(
        topic=args.get("topic", ""),
        reason=args.get("reason", ""),
        callback=kw.get("callback"),
    ),
    emoji="🌱",
)
