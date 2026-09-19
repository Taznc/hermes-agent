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


def test_claude_proxy_preserves_openai_json_object_response_format():
    """Hindsight's soft structured-output mode must reach Anthropic enforcement.

    Anthropic requires ``additionalProperties`` to be explicit on an object
    schema; without it the request 400s, which is terminal (400 is not a
    failover status), so the bridge supplies the permissive-mode default.
    """
    _, raw, _ = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Return valid json only."}],
        "response_format": {"type": "json_object"},
    })

    wire = json.loads(raw)
    assert wire["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {"type": "object", "additionalProperties": False},
        }
    }


def test_claude_proxy_translates_openai_json_schema_response_format():
    """A supplied schema must reach Anthropic without the OpenAI wrapper keys.

    The top-level ``additionalProperties: false`` Anthropic now demands is
    injected; the caller's own dict is never mutated.
    """
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    _, raw, _ = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Grade this."}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "strict": True, "schema": schema},
        },
    })

    wire = json.loads(raw)
    assert wire["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {**schema, "additionalProperties": False},
        }
    }
    assert "additionalProperties" not in schema


@pytest.mark.parametrize("schema,expected", [
    pytest.param(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
        id="object-without-the-key-gains-the-default",
    ),
    pytest.param(
        {"type": "object", "additionalProperties": True},
        {"type": "object", "additionalProperties": True},
        id="explicit-true-is-never-overridden",
    ),
    pytest.param(
        {"type": "object", "additionalProperties": False},
        {"type": "object", "additionalProperties": False},
        id="explicit-false-is-left-alone",
    ),
    pytest.param(
        {"type": "object", "additionalProperties": {"type": "string"}},
        {"type": "object", "additionalProperties": {"type": "string"}},
        id="explicit-subschema-is-left-alone",
    ),
    pytest.param(
        {"type": "array", "items": {"type": "object"}},
        {"type": "array", "items": {"type": "object"}},
        id="non-object-top-level-is-untouched",
    ),
    pytest.param(
        {"type": "object", "properties": {"nested": {"type": "object"}}},
        {
            "type": "object",
            "properties": {"nested": {"type": "object"}},
            "additionalProperties": False,
        },
        id="nested-objects-are-deliberately-not-rewritten",
    ),
    pytest.param(
        {"$ref": "#/definitions/Thing"},
        {"$ref": "#/definitions/Thing"},
        id="schema-without-a-type-is-untouched",
    ),
])
def test_claude_proxy_supplies_additional_properties_conservatively(schema, expected):
    """Anthropic demands the key on object schemas; the bridge adds no more.

    Rewriting a caller's schema tree is a semantic change this bridge has no
    mandate to make, so the injection is scoped to the top-level object and an
    explicit value of any kind survives exactly as written.
    """
    _, raw, _ = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Grade this."}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "o", "schema": schema}},
    })

    assert json.loads(raw)["output_config"]["format"]["schema"] == expected


@pytest.mark.parametrize("response_format", [None, {"type": "text"}])
def test_claude_proxy_omits_output_config_when_no_format_is_requested(response_format):
    """Unconstrained requests must not gain output enforcement they did not ask for."""
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Say hello."}],
    }
    if response_format is not None:
        payload["response_format"] = response_format

    _, raw, _ = prepare_chat_request(payload)

    assert "output_config" not in json.loads(raw)


@pytest.mark.parametrize("response_format", [
    "json_object",
    {},
    {"type": "json"},
    {"type": "json_schema"},
    {"type": "json_schema", "json_schema": "verdict"},
    {"type": "json_schema", "json_schema": {"name": "verdict"}},
    {"type": "json_schema", "json_schema": {"schema": "object"}},
])
def test_claude_proxy_rejects_unsupported_response_format(response_format):
    """Structured-output semantics are never silently weakened to plain prose."""
    with pytest.raises(ValueError, match="response_format"):
        prepare_chat_request({
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "Return json."}],
            "response_format": response_format,
        })


def test_claude_proxy_forces_named_client_tool_through_the_oauth_wire_name():
    """A forced tool must name the wire tool that actually exists upstream."""
    _, raw, tool_name_map = prepare_chat_request({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "look this up"}],
        "tools": [
            {"type": "function", "function": {
                "name": "lookup", "description": "Lookup a record", "parameters": {"type": "object"},
            }},
        ],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    })

    wire = json.loads(raw)
    forced_name = wire["tool_choice"]["name"]
    assert wire["tool_choice"]["type"] == "tool"
    assert forced_name == wire["tools"][0]["name"]
    assert tool_name_map[forced_name] == "lookup"


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


def _reasoning_wire(*, model="claude-sonnet-5", **extra):
    _, raw, _ = prepare_chat_request({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    })
    return raw


def test_claude_proxy_absent_reasoning_effort_is_byte_identical_to_explicit_null():
    """The additive guarantee is on the serialized body, not key presence."""
    without_key = _reasoning_wire()
    explicit_null = _reasoning_wire(reasoning_effort=None)

    assert without_key == explicit_null
    assert b"thinking" not in without_key
    assert b"output_config" not in without_key


@pytest.mark.parametrize("effort,expected", [
    ("low", "low"),
    ("high", "high"),
    ("minimal", "low"),
    ("ultra", "max"),
])
def test_claude_proxy_forwards_client_sent_reasoning_effort(effort, expected):
    wire = json.loads(_reasoning_wire(reasoning_effort=effort))

    assert wire["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert wire["output_config"]["effort"] == expected


def test_claude_proxy_downgrades_xhigh_for_sonnet_4_6():
    """Sonnet/Opus 4.6 400 on ``xhigh``; the shared adapter maps it to max."""
    wire = json.loads(_reasoning_wire(
        model="claude-sonnet-4-6", reasoning_effort="xhigh"
    ))

    assert wire["output_config"]["effort"] == "max"


def test_claude_proxy_leaves_xhigh_on_a_model_that_accepts_it():
    wire = json.loads(_reasoning_wire(
        model="claude-opus-4-8", reasoning_effort="xhigh"
    ))

    assert wire["output_config"]["effort"] == "xhigh"


def test_claude_proxy_omits_thinking_for_haiku_even_when_effort_is_sent():
    """Haiku has no extended thinking; asking must not create an upstream 400."""
    wire = json.loads(_reasoning_wire(
        model="claude-haiku-4-5", reasoning_effort="high"
    ))

    assert "thinking" not in wire
    assert "output_config" not in wire


def test_claude_proxy_merges_effort_and_structured_output_config():
    """Effort and format share output_config; neither may overwrite the other."""
    wire = json.loads(_reasoning_wire(
        reasoning_effort="high", response_format={"type": "json_object"}
    ))

    assert wire["output_config"]["effort"] == "high"
    assert wire["output_config"]["format"]["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("effort", ["extreme", 5, True])
def test_claude_proxy_rejects_unusable_reasoning_effort(effort):
    with pytest.raises(ValueError, match="reasoning_effort"):
        _reasoning_wire(reasoning_effort=effort)


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


@pytest.mark.parametrize("malformed_event", [
    b"data: [1,2,3]\n",
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":99}}\n',
    b'data: {"type":"content_block_delta","delta":"oops"}\n',
    b'data: {"type":"content_block_start","index":0,"content_block":"oops"}\n',
    b'data: {"type":"message_delta","delta":"oops"}\n',
    b'data: {"type":"content_block_delta","index":[],"delta":{"type":"input_json_delta"}}\n',
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","name":[]}}\n',
    b'data: {"type":"message_delta","delta":{"stop_reason":[]}}\n',
])
def test_claude_proxy_stream_fails_closed_with_a_terminal_error_chunk(malformed_event):
    frames = list(stream_events([
        malformed_event,
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"must-not-escape"}}\n',
    ], "claude-sonnet-4-6"))

    data_frames = [frame for frame in frames if frame != b"data: [DONE]\n\n"]
    chunks = [json.loads(frame.removeprefix(b"data: ")) for frame in data_frames]

    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert [chunk["error"]["code"] for chunk in chunks] == ["upstream_invalid_response"]
    assert b"must-not-escape" not in b"".join(frames)
    assert frames[-1] == b"data: [DONE]\n\n"
    assert frames.count(b"data: [DONE]\n\n") == 1
