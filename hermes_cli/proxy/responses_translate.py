"""OpenAI Responses <-> Chat Completions translation for the failover gateway.

The gateway normalizes every request to **Chat Completions** and every backend
answer back to Chat Completions before re-encoding for the client's API family.
One canonical form means N backends and M client families cost N+M translators
instead of N*M, and it lets the already-reviewed Anthropic bridge in
``claude_translate`` serve unchanged as the Claude leg.

Nothing here logs a request or response body.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

# Chat finish_reason <-> Responses status/incomplete reasons.
_FINISH_FOR_RESPONSES = {
    "stop": "completed",
    "tool_calls": "completed",
    "length": "incomplete",
    "content_filter": "incomplete",
}


def _text_from_content(content: Any) -> str:
    """Flatten a Chat or Responses content value into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Client Responses request -> canonical Chat Completions request
# ---------------------------------------------------------------------------


def responses_request_to_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an OpenAI Responses request into a Chat Completions request."""
    if not isinstance(payload, dict):
        raise ValueError("Responses request body must be a JSON object")

    messages: List[Dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or ""),
                        },
                    }],
                })
                continue
            if item_type == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _text_from_content(item.get("output")),
                })
                continue
            role = item.get("role")
            if not isinstance(role, str) or not role:
                continue
            messages.append({"role": role, "content": _text_from_content(item.get("content"))})
    elif raw_input is not None:
        raise ValueError("Responses input must be a string or an array of items")

    if not messages:
        raise ValueError("Responses request requires instructions or input")

    chat: Dict[str, Any] = {
        "model": str(payload.get("model") or ""),
        "messages": messages,
    }

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        converted: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                continue
            # Responses flattens the function fields; Chat nests them.
            nested = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            function: Dict[str, Any] = {"name": str(nested.get("name") or "")}
            description = nested.get("description")
            if isinstance(description, str):
                function["description"] = description
            parameters = nested.get("parameters")
            if isinstance(parameters, dict):
                function["parameters"] = parameters
            converted.append({"type": "function", "function": function})
        if converted:
            chat["tools"] = converted

    tool_choice = payload.get("tool_choice")
    if tool_choice is not None:
        chat["tool_choice"] = tool_choice

    text_config = payload.get("text")
    if isinstance(text_config, dict):
        response_format = _responses_text_format_to_chat(text_config.get("format"))
        if response_format is not None:
            chat["response_format"] = response_format

    max_output_tokens = payload.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool):
        chat["max_tokens"] = max_output_tokens

    for passthrough in ("temperature", "top_p", "stream", "reasoning"):
        if passthrough in payload:
            chat[passthrough] = payload[passthrough]

    return chat


def _responses_text_format_to_chat(fmt: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(fmt, dict):
        return None
    kind = fmt.get("type")
    if kind == "json_schema":
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("Responses json_schema format requires a schema object")
        wrapper: Dict[str, Any] = {"schema": schema}
        name = fmt.get("name")
        if isinstance(name, str) and name:
            wrapper["name"] = name
        strict = fmt.get("strict")
        if isinstance(strict, bool):
            wrapper["strict"] = strict
        return {"type": "json_schema", "json_schema": wrapper}
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "text":
        return {"type": "text"}
    return None


# ---------------------------------------------------------------------------
# Canonical Chat Completions response -> client Responses response
# ---------------------------------------------------------------------------


def chat_response_to_responses(chat: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a Chat Completions response object into a Responses object."""
    if not isinstance(chat, dict):
        raise ValueError("chat completion must be a JSON object")
    choices = chat.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat completion requires a non-empty choices array")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("chat completion choice must be an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("chat completion choice requires a message object")

    output: List[Dict[str, Any]] = []
    text = message.get("content")
    text = text if isinstance(text, str) else ""
    if text:
        output.append({
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })

    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        output.append({
            "id": "fc_" + uuid.uuid4().hex,
            "type": "function_call",
            "status": "completed",
            "call_id": str(call.get("id") or ""),
            "name": str(function.get("name") or ""),
            "arguments": str(function.get("arguments") or ""),
        })

    finish_reason = choice.get("finish_reason")
    status = _FINISH_FOR_RESPONSES.get(str(finish_reason or "stop"), "completed")

    response: Dict[str, Any] = {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": int(chat.get("created") or time.time()),
        "status": status,
        "model": chat.get("model") or "",
        "output": output,
        "output_text": text,
    }
    usage = chat.get("usage")
    if isinstance(usage, dict):
        response["usage"] = {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
    return response


# ---------------------------------------------------------------------------
# Canonical Chat Completions request -> backend Responses request
# ---------------------------------------------------------------------------


def chat_request_to_responses(chat: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a Chat Completions request into an OpenAI Responses request."""
    if not isinstance(chat, dict):
        raise ValueError("chat completion request must be a JSON object")
    messages = chat.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("chat completion request requires a non-empty messages array")

    instructions: List[str] = []
    items: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "developer"}:
            instructions.append(_text_from_content(message.get("content")))
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": _text_from_content(message.get("content")),
            })
            continue
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                })
            text = _text_from_content(message.get("content"))
            if text:
                items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            continue
        content_type = "output_text" if role == "assistant" else "input_text"
        items.append({
            "role": str(role or "user"),
            "content": [{"type": content_type, "text": _text_from_content(message.get("content"))}],
        })

    payload: Dict[str, Any] = {"model": str(chat.get("model") or ""), "input": items}
    joined_instructions = "\n\n".join(part for part in instructions if part)
    if joined_instructions:
        payload["instructions"] = joined_instructions

    tools = chat.get("tools")
    if isinstance(tools, list) and tools:
        converted: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            entry: Dict[str, Any] = {"type": "function", "name": str(function.get("name") or "")}
            description = function.get("description")
            if isinstance(description, str):
                entry["description"] = description
            parameters = function.get("parameters")
            if isinstance(parameters, dict):
                entry["parameters"] = parameters
            converted.append(entry)
        if converted:
            payload["tools"] = converted

    tool_choice = chat.get("tool_choice")
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    text_format = _chat_response_format_to_responses(chat.get("response_format"))
    if text_format is not None:
        payload["text"] = {"format": text_format}

    max_tokens = chat.get("max_completion_tokens", chat.get("max_tokens"))
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        payload["max_output_tokens"] = max_tokens

    for passthrough in ("temperature", "top_p", "stream", "reasoning"):
        if passthrough in chat:
            payload[passthrough] = chat[passthrough]

    return payload


def _chat_response_format_to_responses(response_format: Any) -> Optional[Dict[str, Any]]:
    if response_format is None:
        return None
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be a JSON object")
    kind = response_format.get("type")
    if kind == "text":
        return None
    if kind == "json_object":
        # Responses enforces structure through a schema, so OpenAI's schema-less
        # JSON mode maps to the permissive object schema rather than silently
        # downgrading the caller to free prose.
        return {"type": "json_schema", "schema": {"type": "object"}}
    if kind == "json_schema":
        wrapper = response_format.get("json_schema")
        schema = wrapper.get("schema") if isinstance(wrapper, dict) else None
        if not isinstance(schema, dict):
            raise ValueError("response_format json_schema requires a schema object")
        out: Dict[str, Any] = {"type": "json_schema", "schema": schema}
        name = wrapper.get("name") if isinstance(wrapper, dict) else None
        if isinstance(name, str) and name:
            out["name"] = name
        strict = wrapper.get("strict") if isinstance(wrapper, dict) else None
        if isinstance(strict, bool):
            out["strict"] = strict
        return out
    raise ValueError(f"unsupported response_format type: {kind!r}")


# ---------------------------------------------------------------------------
# Backend Responses response -> canonical Chat Completions response
# ---------------------------------------------------------------------------


def responses_response_to_chat(response: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an OpenAI Responses object into a Chat Completions object."""
    if not isinstance(response, dict):
        raise ValueError("Responses object must be a JSON object")
    output = response.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses object requires an output array")

    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text_parts.append(_text_from_content(item.get("content")))
        elif item_type == "function_call":
            tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or ""),
                },
            })

    content = "".join(text_parts)
    message: Dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    if tool_calls:
        finish_reason = "tool_calls"
    elif response.get("status") == "incomplete":
        finish_reason = "length"
    else:
        finish_reason = "stop"

    chat: Dict[str, Any] = {
        "id": "chatcmpl_" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(response.get("created_at") or time.time()),
        "model": response.get("model") or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    usage = response.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        chat["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
        }
    return chat


# ---------------------------------------------------------------------------
# Streaming: canonical chat chunks -> Responses SSE events
# ---------------------------------------------------------------------------


def _sse(event: str, data: Dict[str, Any]) -> bytes:
    return (
        f"event: {event}\n".encode()
        + b"data: "
        + json.dumps(data, separators=(",", ":")).encode()
        + b"\n\n"
    )


def chat_chunk_to_responses_events(
    chunk: Dict[str, Any], state: Dict[str, Any]
) -> Iterable[bytes]:
    """Translate one ``chat.completion.chunk`` into Responses SSE frames.

    ``state`` is caller-owned per-response scratch space (accumulated text, the
    minted response id, emitted-created flag) so the translator stays a plain
    function and one gateway request cannot leak state into another.
    """
    frames: List[bytes] = []
    if not isinstance(chunk, dict):
        return frames

    if not state.get("created"):
        state["created"] = True
        state["response_id"] = "resp_" + uuid.uuid4().hex
        state["item_id"] = "msg_" + uuid.uuid4().hex
        state["text"] = ""
        state["tool_calls"] = {}
        state["model"] = chunk.get("model") or ""
        frames.append(_sse("response.created", {
            "type": "response.created",
            "response": {
                "id": state["response_id"],
                "object": "response",
                "status": "in_progress",
                "model": state["model"],
                "output": [],
            },
        }))

    choices = chunk.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(choice, dict):
        choice = {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

    text_delta = delta.get("content")
    if isinstance(text_delta, str) and text_delta:
        state["text"] = state.get("text", "") + text_delta
        frames.append(_sse("response.output_text.delta", {
            "type": "response.output_text.delta",
            "response_id": state["response_id"],
            "item_id": state["item_id"],
            "output_index": 0,
            "content_index": 0,
            "delta": text_delta,
        }))

    for call in delta.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        index = call.get("index", 0)
        entry = state["tool_calls"].setdefault(
            index, {"call_id": "", "name": "", "arguments": ""}
        )
        if call.get("id"):
            entry["call_id"] = str(call["id"])
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name"):
            entry["name"] = str(function["name"])
        argument_delta = function.get("arguments")
        if isinstance(argument_delta, str) and argument_delta:
            entry["arguments"] += argument_delta
            frames.append(_sse("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "response_id": state["response_id"],
                "item_id": state["item_id"],
                "output_index": int(index) if isinstance(index, int) else 0,
                "delta": argument_delta,
            }))

    if choice.get("finish_reason") and not state.get("completed"):
        state["completed"] = True
        output: List[Dict[str, Any]] = []
        if state.get("text"):
            output.append({
                "id": state["item_id"],
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": state["text"], "annotations": []}
                ],
            })
        for _, entry in sorted(state["tool_calls"].items(), key=lambda pair: str(pair[0])):
            output.append({
                "id": "fc_" + uuid.uuid4().hex,
                "type": "function_call",
                "status": "completed",
                "call_id": entry["call_id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
            })
        status = _FINISH_FOR_RESPONSES.get(str(choice.get("finish_reason")), "completed")
        response: Dict[str, Any] = {
            "id": state["response_id"],
            "object": "response",
            "status": status,
            "model": state.get("model") or "",
            "output": output,
            "output_text": state.get("text", ""),
        }
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            response["usage"] = {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
            }
        frames.append(_sse("response.completed", {
            "type": "response.completed",
            "response": response,
        }))

    return frames


class ResponsesStreamTranslator:
    """Stateful Responses-SSE-to-Chat-chunk translator for one upstream response.

    Mirrors ``claude_translate.ClaudeStreamTranslator`` so both backend legs
    produce the same canonical ``chat.completion.chunk`` stream, which is what
    lets one client-facing encoder serve either backend.
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._chunk_id = "chatcmpl_" + uuid.uuid4().hex
        self._tool_index_by_output: Dict[Any, int] = {}
        self._next_tool_index = 0
        self._finished = False

    def _chunk(self, delta: Dict[str, Any], finish_reason: Optional[str]) -> Dict[str, Any]:
        return {
            "id": self._chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def translate(self, raw: bytes) -> Iterable[Dict[str, Any]]:
        """Translate one upstream SSE line into zero or more chat chunks."""
        if not raw.startswith(b"data:"):
            return
        payload = raw[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            event = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return

        event_type = event.get("type")

        if event_type == "response.output_text.delta":
            text = event.get("delta")
            if isinstance(text, str) and text:
                yield self._chunk({"content": text}, None)
            return

        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                index = self._tool_index_for(event.get("output_index"))
                yield self._chunk({
                    "tool_calls": [{
                        "index": index,
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": "",
                        },
                    }]
                }, None)
            return

        if event_type == "response.function_call_arguments.delta":
            argument_delta = event.get("delta")
            if isinstance(argument_delta, str) and argument_delta:
                index = self._tool_index_for(event.get("output_index"))
                yield self._chunk({
                    "tool_calls": [{
                        "index": index,
                        "function": {"arguments": argument_delta},
                    }]
                }, None)
            return

        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            if self._finished:
                return
            self._finished = True
            raw_response = event.get("response")
            response = raw_response if isinstance(raw_response, dict) else {}
            if (response or {}).get("status") == "incomplete" or event_type == "response.incomplete":
                finish_reason = "length"
            elif self._tool_index_by_output:
                finish_reason = "tool_calls"
            else:
                finish_reason = "stop"
            chunk = self._chunk({}, finish_reason)
            usage = response.get("usage")
            if isinstance(usage, dict):
                prompt_tokens = int(usage.get("input_tokens") or 0)
                completion_tokens = int(usage.get("output_tokens") or 0)
                chunk["usage"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
                }
            yield chunk

    def _tool_index_for(self, output_index: Any) -> int:
        key = output_index if output_index is not None else "_default"
        if key not in self._tool_index_by_output:
            self._tool_index_by_output[key] = self._next_tool_index
            self._next_tool_index += 1
        return self._tool_index_by_output[key]


__all__ = [
    "ResponsesStreamTranslator",
    "chat_chunk_to_responses_events",
    "chat_request_to_responses",
    "chat_response_to_responses",
    "responses_request_to_chat",
    "responses_response_to_chat",
]
