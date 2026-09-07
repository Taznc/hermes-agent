"""OpenAI Chat Completions <-> Anthropic Messages translation for the proxy.

This is intentionally a narrow compatibility bridge: only the Chat Completions
fields needed by OpenAI-compatible callers are accepted and request bodies are
never logged.  Existing Hermes conversion code owns tool/message normalization.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, cast

from agent.anthropic_adapter import build_anthropic_kwargs

_ANTHROPIC_VERSION = "2023-06-01"
_OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20"
_STOP_REASONS = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop"}


def prepare_chat_request(payload: Dict[str, Any]) -> tuple[Dict[str, str], bytes]:
    """Return Anthropic request headers/body for an OpenAI chat payload."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("OpenAI chat completion requires a non-empty messages array")
    model = str(payload.get("model") or "claude-sonnet-4-6").strip()
    if not model:
        raise ValueError("OpenAI chat completion requires model")
    max_tokens = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if not isinstance(max_tokens, (int, float)) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = 16384
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        function_raw = tool_choice.get("function")
        function = cast(Dict[str, Any], function_raw) if isinstance(function_raw, dict) else {}
        tool_choice = function.get("name") if tool_choice.get("type") == "function" else tool_choice.get("type")
    kwargs = build_anthropic_kwargs(
        model=model,
        messages=messages,
        tools=payload.get("tools") if isinstance(payload.get("tools"), list) else None,
        max_tokens=int(max_tokens),
        reasoning_config=None,
        tool_choice=tool_choice if isinstance(tool_choice, str) else None,
        is_oauth=True,
        base_url="https://api.anthropic.com/v1",
    )
    kwargs["stream"] = bool(payload.get("stream"))
    # SDK-only helpers must never cross the raw HTTP boundary.
    kwargs.pop("extra_headers", None)
    return {"anthropic-version": _ANTHROPIC_VERSION, "anthropic-beta": _OAUTH_BETAS}, json.dumps(kwargs).encode("utf-8")


def response_to_openai(message: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a completed Anthropic Message JSON to Chat Completions JSON."""
    content, tool_calls = [], []
    for block in message.get("content") or []:
        if block.get("type") == "text":
            content.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({"id": block.get("id") or f"call_{uuid.uuid4().hex}", "type": "function", "function": {
                "name": block.get("name", ""), "arguments": json.dumps(block.get("input") or {}, separators=(",", ":")),
            }})
    usage = message.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    assistant: Dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if tool_calls:
        assistant["tool_calls"] = tool_calls
    return {"id": "chatcmpl_" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
            "model": message.get("model", ""), "choices": [{"index": 0, "message": assistant,
            "finish_reason": _STOP_REASONS.get(message.get("stop_reason"), "stop")}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}}


def stream_events(lines: Iterable[bytes], model: str, *, final: bool = True) -> Iterable[bytes]:
    """Translate Anthropic SSE event lines to OpenAI SSE frames.

    Anthropic sends JSON in ``data:`` lines; unknown events are intentionally
    ignored so newly-added metadata cannot leak malformed OpenAI chunks.
    """
    call_index = 0
    for raw in lines:
        if not raw.startswith(b"data:"):
            continue
        try:
            event = json.loads(raw[5:].strip())
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        typ = event.get("type")
        delta: Dict[str, Any] = {}
        if typ == "content_block_delta":
            part = event.get("delta") or {}
            if part.get("type") == "text_delta":
                delta["content"] = part.get("text", "")
            elif part.get("type") == "input_json_delta":
                delta["tool_calls"] = [{"index": call_index, "function": {"arguments": part.get("partial_json", "")}}]
        elif typ == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                delta["tool_calls"] = [{"index": call_index, "id": block.get("id"), "type": "function",
                    "function": {"name": block.get("name", ""), "arguments": ""}}]
                call_index += 1
        elif typ == "message_delta":
            delta["content"] = ""
            finish = _STOP_REASONS.get((event.get("delta") or {}).get("stop_reason"), "stop")
            chunk = {"id": "chatcmpl_proxy", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            yield b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"
            continue
        if delta:
            chunk = {"id": "chatcmpl_proxy", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            yield b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"
    if final:
        yield b"data: [DONE]\n\n"
