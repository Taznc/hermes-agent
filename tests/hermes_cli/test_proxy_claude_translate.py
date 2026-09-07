"""Contracts for the Claude Code subscription proxy translation."""
import json

import pytest

from hermes_cli.proxy.adapters.claude_code import ClaudeCodeAdapter
from hermes_cli.proxy.claude_translate import prepare_chat_request, response_to_openai, stream_events
from hermes_cli.proxy.server import create_app


def test_claude_subscription_proxy_is_loopback_only_and_requires_client_authority():
    adapter = ClaudeCodeAdapter()
    assert adapter.loopback_only is True
    assert adapter.requires_client_auth is True
    assert adapter.allowed_paths == frozenset({"/chat/completions"})
    with pytest.raises(RuntimeError, match="client authentication"):
        create_app(adapter)


def test_claude_proxy_translates_tool_request_and_response():
    headers, raw, tool_name_map = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "look this up"}],
        "tools": [
            {"type": "function", "function": {
                "name": "lookup", "description": "Lookup a record", "parameters": {"type": "object"},
            }},
            {"type": "function", "function": {
                "name": "session_search", "description": "Search sessions", "parameters": {"type": "object"},
            }},
        ],
        "tool_choice": "required",
    })
    wire = json.loads(raw)
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["user-agent"].startswith("claude-code/")
    assert headers["x-app"] == "cli"
    assert wire["tools"][0]["name"].startswith("mcp__")
    translated = response_to_openai({
        "model": "claude-sonnet-4-6", "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "mcp__lookup", "input": {"id": 7}},
            {"type": "tool_use", "id": "toolu_2", "name": "mcp__chat_history_lookup", "input": {"query": "prior"}},
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }, tool_name_map=tool_name_map)
    choice = translated["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "lookup"
    assert choice["message"]["tool_calls"][1]["function"]["name"] == "session_search"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"id": 7}


def test_claude_proxy_reverses_oauth_tool_aliases_in_streaming_response():
    _, _, tool_name_map = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "find prior context"}],
        "tools": [
            {"type": "function", "function": {
                "name": "lookup", "description": "Lookup a record", "parameters": {"type": "object"},
            }},
            {"type": "function", "function": {
                "name": "session_search", "description": "Search prior sessions", "parameters": {"type": "object"},
            }},
        ],
    })
    frames = list(stream_events([
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"mcp__lookup","input":{}}}\n',
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_2","name":"mcp__chat_history_lookup","input":{}}}\n',
    ], "claude-sonnet-4-6", tool_name_map=tool_name_map))
    assert b'"name":"lookup"' in frames[0]
    assert b'"name":"session_search"' in frames[1]


def test_claude_proxy_translates_stream_and_terminates_once():
    frames = list(stream_events([
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n',
    ], "claude-sonnet-4-6"))
    assert b'"content":"hello"' in frames[0]
    assert b'"finish_reason":"stop"' in frames[1]
    assert frames[-1] == b"data: [DONE]\n\n"
