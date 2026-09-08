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

from agent.anthropic_adapter import _get_claude_code_version, build_anthropic_kwargs

_ANTHROPIC_VERSION = "2023-06-01"
_OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20"
_STOP_REASONS = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop"}


def _wire_to_client_tool_names(payload_tools: Any, anthropic_tools: Any) -> Dict[str, str]:
    """Map each generated OAuth wire name back to the client's original name.

    The proxy cannot consult Hermes's local tool registry: OpenAI clients can
    register arbitrary tools.  Keep only the same first occurrence that the
    shared converter retains, then pair it with the already-normalized outbound
    Anthropic tool list so aliases and prefixing are reversed exactly.
    """
    client_names: list[str] = []
    seen_names: set[str] = set()
    for tool in payload_tools if isinstance(payload_tools, list) else []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name", "") if isinstance(function, dict) else ""
        name = name if isinstance(name, str) else ""
        if name and name in seen_names:
            continue
        if name:
            seen_names.add(name)
        client_names.append(name)
    wire_names = [
        tool.get("name") for tool in anthropic_tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ] if isinstance(anthropic_tools, list) else []
    return {wire_name: client_name for client_name, wire_name in zip(client_names, wire_names) if wire_name}


def _anthropic_output_format(response_format: Any) -> Dict[str, Any] | None:
    """Translate an OpenAI ``response_format`` to an Anthropic output format.

    Anthropic enforces structured output through a JSON Schema, so OpenAI's
    schema-less ``json_object`` mode maps to the permissive ``{"type": "object"}``
    schema.  ``text`` is OpenAI's explicit unconstrained mode and correctly
    produces no enforcement.  Anything this bridge cannot express faithfully
    raises instead of silently downgrading the caller to free prose.

    ``agent.auxiliary_client._translate_anthropic_response_format`` performs the
    same mapping for in-process SDK calls but drops shapes it cannot handle,
    which is right there (a local caller keeps its own retry ladder) and wrong
    here: a proxy client has already been told its request was accepted.
    """
    if response_format is None:
        return None
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be a JSON object")
    kind = response_format.get("type")
    if kind == "text":
        return None
    if kind == "json_object":
        return {"type": "json_schema", "schema": {"type": "object"}}
    if kind == "json_schema":
        wrapper = response_format.get("json_schema")
        schema = wrapper.get("schema") if isinstance(wrapper, dict) else None
        if not isinstance(schema, dict):
            raise ValueError("response_format json_schema requires a schema object")
        # OpenAI-only wrapper keys (name/strict/description) have no Anthropic
        # equivalent and are 400s upstream, so only the schema travels.
        return {"type": "json_schema", "schema": schema}
    raise ValueError(f"unsupported response_format type: {kind!r}")


def prepare_chat_request(payload: Dict[str, Any]) -> tuple[Dict[str, str], bytes, Dict[str, str]]:
    """Return Anthropic request headers/body and wire-to-client tool-name mapping."""
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
    output_format = _anthropic_output_format(payload.get("response_format"))
    if output_format is not None:
        # ``output_config`` may already carry a thinking effort; only the format
        # key belongs to the client's requested response format.
        kwargs.setdefault("output_config", {})["format"] = output_format
    # SDK-only helpers must never cross the raw HTTP boundary.
    kwargs.pop("extra_headers", None)
    # Anthropic routes subscription OAuth by the official Claude Code identity;
    # see ``build_anthropic_client(... style == "oauth")`` for the shared contract.
    headers = {
        "anthropic-version": _ANTHROPIC_VERSION,
        "anthropic-beta": _OAUTH_BETAS,
        "user-agent": f"claude-code/{_get_claude_code_version()} (external, cli)",
        "x-app": "cli",
    }
    return headers, json.dumps(kwargs).encode("utf-8"), _wire_to_client_tool_names(payload.get("tools"), kwargs.get("tools"))


def response_to_openai(message: Dict[str, Any], *, tool_name_map: Dict[str, str] | None = None) -> Dict[str, Any]:
    """Translate a completed Anthropic Message JSON to Chat Completions JSON."""
    blocks = message.get("content")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Anthropic message content must be a non-empty array")
    content, tool_calls = [], []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Anthropic message content blocks must be objects")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Anthropic text blocks require string text")
            content.append(text)
        elif block_type == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            tool_input = block.get("input")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Anthropic tool_use blocks require a non-empty string id")
            if not isinstance(name, str) or not name:
                raise ValueError("Anthropic tool_use blocks require a non-empty string name")
            if not isinstance(tool_input, dict):
                raise ValueError("Anthropic tool_use blocks require an object input")
            tool_calls.append({"id": call_id, "type": "function", "function": {
                "name": (tool_name_map or {}).get(name, name),
                "arguments": json.dumps(tool_input, separators=(",", ":")),
            }})
        else:
            raise ValueError(f"unsupported Anthropic content block type: {block_type!r}")
    usage = message.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("Anthropic message usage must be an object")
    token_counts = []
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Anthropic usage {field} must be a non-negative integer")
        token_counts.append(value)
    prompt_tokens, completion_tokens = token_counts
    model = message.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Anthropic message model must be a non-empty string")
    stop_reason = message.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        raise ValueError("Anthropic message stop_reason must be a string or null")
    assistant: Dict[str, Any] = {"role": "assistant", "content": "".join(content) if content else None}
    if tool_calls:
        assistant["tool_calls"] = tool_calls
    return {"id": "chatcmpl_" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": assistant,
            "finish_reason": _STOP_REASONS.get(stop_reason, "stop") if stop_reason is not None else "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}}


class ClaudeStreamTranslator:
    """Stateful Anthropic SSE-to-OpenAI translator for one upstream response."""

    def __init__(self, model: str, *, tool_name_map: Dict[str, str] | None = None) -> None:
        self._model = model
        self._tool_name_map = tool_name_map or {}
        self._call_index = 0
        self._tool_call_by_content_block: Dict[int, int] = {}
        self._active_tool_call_index: int | None = None
        self._failed = False

    def _invalid_response_frame(self) -> bytes:
        """Emit the only useful failure signal after SSE headers are committed."""
        self._failed = True
        code = "upstream_invalid_response"
        chunk = {
            "id": "chatcmpl_proxy",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self._model,
            "choices": [],
            "error": {
                "message": "upstream returned a success status with an unusable body",
                "type": code,
                "code": code,
            },
        }
        return b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"

    def translate(self, raw: bytes) -> Iterable[bytes]:
        """Translate one Anthropic SSE line while retaining tool-call position."""
        if self._failed or not raw.startswith(b"data:"):
            return
        try:
            event_raw = json.loads(raw[5:].strip())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event_raw, dict):
            yield self._invalid_response_frame()
            return
        event = cast(Any, event_raw)
        typ = event.get("type")
        delta: Dict[str, Any] = {}
        if typ == "content_block_delta":
            part = event.get("delta")
            if not isinstance(part, dict):
                yield self._invalid_response_frame()
                return
            if part.get("type") == "text_delta":
                text = part.get("text", "")
                if not isinstance(text, str):
                    yield self._invalid_response_frame()
                    return
                delta["content"] = text
            elif part.get("type") == "input_json_delta":
                content_index = event.get("index")
                tool_call_index = self._tool_call_by_content_block.get(
                    content_index,
                    self._active_tool_call_index,
                )
                if tool_call_index is not None:
                    delta["tool_calls"] = [{"index": tool_call_index, "function": {"arguments": part.get("partial_json", "")}}]
        elif typ == "content_block_start":
            block = event.get("content_block")
            if not isinstance(block, dict):
                yield self._invalid_response_frame()
                return
            if block.get("type") == "tool_use":
                tool_call_index = self._call_index
                content_index = event.get("index")
                if isinstance(content_index, int):
                    self._tool_call_by_content_block[content_index] = tool_call_index
                self._active_tool_call_index = tool_call_index
                delta["tool_calls"] = [{"index": tool_call_index, "id": block.get("id"), "type": "function",
                    "function": {"name": self._tool_name_map.get(block.get("name", ""), block.get("name", "")), "arguments": ""}}]
                self._call_index += 1
        elif typ == "message_delta":
            message_delta_raw = event.get("delta")
            if not isinstance(message_delta_raw, dict):
                yield self._invalid_response_frame()
                return
            message_delta = cast(Dict[str, Any], message_delta_raw)
            delta["content"] = ""
            stop_reason = cast(str, message_delta.get("stop_reason"))
            finish = _STOP_REASONS.get(stop_reason, "stop")
            chunk = {"id": "chatcmpl_proxy", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": self._model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            yield b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"
            return
        if delta:
            chunk = {"id": "chatcmpl_proxy", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": self._model, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            yield b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"


def stream_events(
    lines: Iterable[bytes], model: str, *, final: bool = True, tool_name_map: Dict[str, str] | None = None,
) -> Iterable[bytes]:
    """Translate one complete Anthropic SSE sequence to OpenAI SSE frames."""
    translator = ClaudeStreamTranslator(model, tool_name_map=tool_name_map)
    for raw in lines:
        yield from translator.translate(raw)
    if final:
        yield b"data: [DONE]\n\n"
