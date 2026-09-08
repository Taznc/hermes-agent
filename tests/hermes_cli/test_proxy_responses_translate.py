"""Contract tests for the OpenAI Responses <-> Chat Completions translation.

Chat Completions is the gateway's canonical internal form, so these two
directions are what make a Responses client and a Responses backend usable
against a Chat-Completions backend and vice versa.
"""

from __future__ import annotations

import json

from hermes_cli.proxy.responses_translate import (
    chat_chunk_to_responses_events,
    chat_request_to_responses,
    chat_response_to_responses,
    responses_request_to_chat,
    responses_response_to_chat,
)


# --------------------------------------------------------------------------
# Client Responses request -> canonical chat request
# --------------------------------------------------------------------------


def test_responses_request_string_input_becomes_a_user_message():
    chat = responses_request_to_chat({"model": "m", "input": "hello there"})
    assert chat["model"] == "m"
    assert chat["messages"] == [{"role": "user", "content": "hello there"}]
    assert chat.get("stream") is not True


def test_responses_request_instructions_become_a_leading_system_message():
    chat = responses_request_to_chat(
        {"model": "m", "instructions": "be terse", "input": "hi"}
    )
    assert chat["messages"][0] == {"role": "system", "content": "be terse"}
    assert chat["messages"][1] == {"role": "user", "content": "hi"}


def test_responses_request_structured_input_items_map_to_messages():
    chat = responses_request_to_chat({
        "model": "m",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "part one "}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "ack"}]},
            {"role": "user", "content": "part two"},
        ],
    })
    assert chat["messages"] == [
        {"role": "user", "content": "part one "},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "part two"},
    ]


def test_responses_request_tools_map_to_chat_function_tools():
    chat = responses_request_to_chat({
        "model": "m",
        "input": "go",
        "tools": [{
            "type": "function",
            "name": "lookup",
            "description": "look something up",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }],
    })
    assert chat["tools"] == [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "look something up",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }]


def test_responses_request_text_format_maps_to_response_format():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    chat = responses_request_to_chat({
        "model": "m",
        "input": "go",
        "text": {"format": {"type": "json_schema", "name": "out", "schema": schema}},
    })
    assert chat["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "out", "schema": schema},
    }


def test_responses_request_max_output_tokens_maps_to_max_tokens():
    chat = responses_request_to_chat(
        {"model": "m", "input": "go", "max_output_tokens": 256, "stream": True}
    )
    assert chat["max_tokens"] == 256
    assert chat["stream"] is True


# --------------------------------------------------------------------------
# Canonical chat response -> client Responses response
# --------------------------------------------------------------------------


def test_chat_response_text_becomes_a_responses_message_output_item():
    responses = chat_response_to_responses({
        "id": "chatcmpl_1",
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "the answer"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    })
    assert responses["object"] == "response"
    assert responses["status"] == "completed"
    assert responses["model"] == "m"
    assert responses["output"] == [{
        "id": responses["output"][0]["id"],
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "the answer", "annotations": []}],
    }]
    assert responses["output_text"] == "the answer"
    assert responses["usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }


def test_chat_response_tool_calls_become_function_call_output_items():
    responses = chat_response_to_responses({
        "id": "chatcmpl_2",
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })
    assert responses["output"] == [{
        "id": responses["output"][0]["id"],
        "type": "function_call",
        "status": "completed",
        "call_id": "call_a",
        "name": "lookup",
        "arguments": '{"q":"x"}',
    }]
    assert responses["output_text"] == ""


# --------------------------------------------------------------------------
# Canonical chat request -> backend Responses request (Codex leg)
# --------------------------------------------------------------------------


def test_chat_request_messages_become_responses_input_items():
    payload = chat_request_to_responses({
        "model": "m",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
    })
    assert payload["instructions"] == "be terse"
    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_chat_request_assistant_tool_calls_round_trip_to_responses_items():
    payload = chat_request_to_responses({
        "model": "m",
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "result text"},
        ],
    })
    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call_a",
        "name": "lookup",
        "arguments": '{"q":"x"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_a",
        "output": "result text",
    }


def test_chat_request_tools_and_response_format_translate_to_responses_shape():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    payload = chat_request_to_responses({
        "model": "m",
        "messages": [{"role": "user", "content": "go"}],
        "tools": [{
            "type": "function",
            "function": {"name": "lookup", "description": "d", "parameters": schema},
        }],
        "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
        "max_tokens": 128,
    })
    assert payload["tools"] == [
        {"type": "function", "name": "lookup", "description": "d", "parameters": schema}
    ]
    assert payload["text"] == {
        "format": {"type": "json_schema", "name": "out", "schema": schema}
    }
    assert payload["max_output_tokens"] == 128


def test_chat_request_json_object_response_format_becomes_permissive_schema():
    payload = chat_request_to_responses({
        "model": "m",
        "messages": [{"role": "user", "content": "go"}],
        "response_format": {"type": "json_object"},
    })
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["schema"] == {"type": "object"}


# --------------------------------------------------------------------------
# Backend Responses response -> canonical chat response (Codex leg)
# --------------------------------------------------------------------------


def test_responses_response_text_and_tool_calls_become_a_chat_completion():
    chat = responses_response_to_chat({
        "id": "resp_1",
        "model": "gpt-5",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
            },
            {
                "type": "function_call",
                "call_id": "call_a",
                "name": "lookup",
                "arguments": '{"q":"x"}',
            },
        ],
        "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
    })
    choice = chat["choices"][0]
    assert chat["object"] == "chat.completion"
    assert chat["model"] == "gpt-5"
    assert choice["message"]["content"] == "hello"
    assert choice["message"]["tool_calls"] == [{
        "id": "call_a",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"x"}'},
    }]
    assert choice["finish_reason"] == "tool_calls"
    assert chat["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 6,
        "total_tokens": 11,
    }


def test_responses_response_without_tool_calls_finishes_with_stop():
    chat = responses_response_to_chat({
        "id": "resp_2",
        "model": "gpt-5",
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        }],
    })
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in chat["choices"][0]["message"]


# --------------------------------------------------------------------------
# Streaming: canonical chat chunks -> Responses SSE events
# --------------------------------------------------------------------------


def test_chat_stream_chunks_translate_to_responses_sse_events():
    emitted: list[bytes] = []
    state: dict = {}
    for chunk in [
        {
            "id": "chatcmpl_s",
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": "he"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl_s",
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl_s",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]:
        emitted.extend(chat_chunk_to_responses_events(chunk, state))

    text = b"".join(emitted).decode()
    assert "response.created" in text
    assert "response.output_text.delta" in text
    assert "response.completed" in text

    deltas = [
        json.loads(line[len("data: "):])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]
    assert "".join(
        event.get("delta", "")
        for event in deltas
        if event.get("type") == "response.output_text.delta"
    ) == "hello"

    completed = [event for event in deltas if event.get("type") == "response.completed"]
    assert len(completed) == 1
    assert completed[0]["response"]["status"] == "completed"
    assert completed[0]["response"]["output"][0]["content"][0]["text"] == "hello"
