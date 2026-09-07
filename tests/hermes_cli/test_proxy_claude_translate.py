"""Contracts for the Claude Code subscription proxy translation."""
import json

from hermes_cli.proxy.claude_translate import prepare_chat_request, response_to_openai, stream_events


def test_claude_proxy_translates_tool_request_and_response():
    headers, raw = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "look this up"}],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "description": "Lookup a record", "parameters": {"type": "object"},
        }}],
        "tool_choice": "required",
    })
    wire = json.loads(raw)
    assert headers["anthropic-version"] == "2023-06-01"
    assert wire["tools"][0]["name"].startswith("mcp__")
    translated = response_to_openai({
        "model": "claude-sonnet-4-6", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"id": 7}}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    })
    choice = translated["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "lookup"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"id": 7}


def test_claude_proxy_translates_stream_and_terminates_once():
    frames = list(stream_events([
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n',
    ], "claude-sonnet-4-6"))
    assert b'"content":"hello"' in frames[0]
    assert b'"finish_reason":"stop"' in frames[1]
    assert frames[-1] == b"data: [DONE]\n\n"
