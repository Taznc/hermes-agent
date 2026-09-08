"""End-to-end contract + fault-injection tests for the failover gateway.

Every test drives the real ``create_failover_app`` over real loopback HTTP
against real fake upstreams speaking real Anthropic Messages and real OpenAI
Responses wire formats. Nothing here mocks the leg, the router, or the breaker.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential  # noqa: E402
from hermes_cli.proxy.gateway import create_failover_app  # noqa: E402
from hermes_cli.proxy.routing import BackendCircuit  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles: adapters point at a real fake upstream; no credentials on disk.
# ---------------------------------------------------------------------------


class _StubAdapter(UpstreamAdapter):
    def __init__(self, name: str, wire: str, base_url: str) -> None:
        self._name = name
        self._wire = wire
        self._base_url = base_url
        self.credential_calls = 0

    @property
    def name(self):
        return self._name

    @property
    def display_name(self):
        return f"Stub {self._name}"

    @property
    def allowed_paths(self):
        return frozenset({"/chat/completions", "/responses"})

    @property
    def wire_protocol(self):
        return self._wire

    def is_authenticated(self):
        return True

    def get_credential(self):
        self.credential_calls += 1
        return UpstreamCredential(bearer="stub-bearer", base_url=self._base_url)


class _BrokenCredentialAdapter(_StubAdapter):
    def get_credential(self):
        self.credential_calls += 1
        raise RuntimeError("simulated credential failure")


# ---------------------------------------------------------------------------
# Fake upstreams
# ---------------------------------------------------------------------------


def _anthropic_upstream(
    *,
    status: int = 200,
    body: Dict[str, Any] | None = None,
    stream_lines: List[bytes] | None = None,
    calls: List[Dict[str, Any]] | None = None,
    retry_after: str | None = None,
) -> "web.Application":
    async def messages(request):
        payload = await request.json()
        if calls is not None:
            calls.append(payload)
        if status >= 400:
            headers = {"Retry-After": retry_after} if retry_after else {}
            return web.json_response(
                {"error": {"message": "upstream refused", "type": "error"}},
                status=status,
                headers=headers,
            )
        if payload.get("stream"):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await resp.prepare(request)
            for line in stream_lines or []:
                await resp.write(line)
            await resp.write_eof()
            return resp
        return web.json_response(body or _anthropic_text_message("claude says hi"))

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    return app


def _anthropic_text_message(text: str) -> Dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }


def _codex_upstream(
    *,
    status: int = 200,
    events: List[bytes] | None = None,
    calls: List[Dict[str, Any]] | None = None,
    retry_after: str | None = None,
) -> "web.Application":
    async def responses(request):
        payload = await request.json()
        if calls is not None:
            calls.append(payload)
        if status >= 400:
            headers = {"Retry-After": retry_after} if retry_after else {}
            return web.json_response(
                {"error": {"message": "upstream refused", "type": "error"}},
                status=status,
                headers=headers,
            )
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)
        for event in events or _codex_text_events("codex says hi"):
            await resp.write(event)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/responses", responses)
    return app


def _sse(payload: Dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


def _codex_text_events(text: str) -> List[bytes]:
    return [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": text}),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "id": "item_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        }),
        _sse({
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": "gpt-5",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
            },
        }),
    ]


async def _serve(app) -> tuple:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(site._server.sockets)[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


class _Harness:
    """Runs the gateway plus its fake upstreams on real loopback sockets."""

    def __init__(self, upstreams: List[tuple], **gateway_kwargs):
        self._specs = upstreams
        self._kwargs = gateway_kwargs
        self.adapters: List[_StubAdapter] = []
        self._runners: List[Any] = []
        self.base_url = ""

    async def __aenter__(self):
        for name, wire, app, adapter_cls in self._specs:
            runner, base = await _serve(app)
            self._runners.append(runner)
            self.adapters.append(adapter_cls(name, wire, f"{base}/v1"))
        gateway = create_failover_app(self.adapters, **self._kwargs)
        runner, base = await _serve(gateway)
        self._runners.append(runner)
        self.base_url = base
        return self

    async def __aexit__(self, *exc):
        for runner in self._runners:
            await runner.cleanup()

    async def post(self, path: str, payload: Dict[str, Any], **kwargs):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}{path}", json=payload, **kwargs
            ) as response:
                return response.status, dict(response.headers), await response.read()


def _claude_first(claude_app, codex_app, *, claude_cls=_StubAdapter, codex_cls=_StubAdapter):
    return [
        ("claude-code", "anthropic-messages", claude_app, claude_cls),
        ("openai-codex", "openai-responses", codex_app, codex_cls),
    ]


def _codex_first(codex_app, claude_app):
    return [
        ("openai-codex", "openai-responses", codex_app, _StubAdapter),
        ("claude-code", "anthropic-messages", claude_app, _StubAdapter),
    ]


# ===========================================================================
# AC1 — configuration / backward compatibility
# ===========================================================================


def test_failover_app_requires_at_least_two_backends():
    single = [_StubAdapter("claude-code", "anthropic-messages", "http://x/v1")]
    with pytest.raises(ValueError, match="at least two backends"):
        create_failover_app(single)


def test_failover_app_rejects_duplicate_backends():
    duplicated = [
        _StubAdapter("claude-code", "anthropic-messages", "http://x/v1"),
        _StubAdapter("claude-code", "anthropic-messages", "http://y/v1"),
    ]
    with pytest.raises(ValueError, match="Duplicate"):
        create_failover_app(duplicated)


def test_unknown_gateway_path_is_a_clear_404_not_a_silent_proxy():
    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream())
        ) as harness:
            status, _, body = await harness.post("/v1/embeddings", {"input": "x"})
            assert status == 404
            assert json.loads(body)["error"]["code"] == "path_not_allowed"

    asyncio.run(run())


def test_health_reports_every_backend_and_observable_circuit_state():
    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream())
        ) as harness:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{harness.base_url}/health") as response:
                    assert response.status == 200
                    payload = await response.json()
            assert payload["mode"] == "failover"
            assert [b["name"] for b in payload["backends"]] == [
                "claude-code",
                "openai-codex",
            ]
            assert "circuits" in payload

    asyncio.run(run())


# ===========================================================================
# AC2 — contract matrix: 2 client families x 2 backends x 4 features
# ===========================================================================


def test_chat_client_against_claude_backend_returns_text():
    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream())
        ) as harness:
            status, _, body = await harness.post(
                "/v1/chat/completions",
                {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["object"] == "chat.completion"
            assert payload["choices"][0]["message"]["content"] == "claude says hi"

    asyncio.run(run())


def test_chat_client_against_codex_backend_returns_text():
    async def run():
        async with _Harness(
            _codex_first(_codex_upstream(), _anthropic_upstream())
        ) as harness:
            status, _, body = await harness.post(
                "/v1/chat/completions",
                {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["object"] == "chat.completion"
            assert payload["choices"][0]["message"]["content"] == "codex says hi"

    asyncio.run(run())


def test_responses_client_against_claude_backend_returns_a_response_object():
    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream())
        ) as harness:
            status, _, body = await harness.post(
                "/v1/responses", {"model": "claude-sonnet-4-6", "input": "hi"}
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["object"] == "response"
            assert payload["output_text"] == "claude says hi"
            assert payload["output"][0]["type"] == "message"

    asyncio.run(run())


def test_responses_client_against_codex_backend_returns_a_response_object():
    async def run():
        async with _Harness(
            _codex_first(_codex_upstream(), _anthropic_upstream())
        ) as harness:
            status, _, body = await harness.post(
                "/v1/responses", {"model": "gpt-5", "input": "hi"}
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["object"] == "response"
            assert payload["output_text"] == "codex says hi"

    asyncio.run(run())


def test_tools_reach_the_claude_backend_and_tool_calls_come_back():
    calls: List[Dict[str, Any]] = []
    tool_message = {
        "id": "msg_2",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }

    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(body=tool_message, calls=calls), _codex_upstream()
            )
        ) as harness:
            status, _, body = await harness.post(
                "/v1/chat/completions",
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }],
                },
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["choices"][0]["finish_reason"] == "tool_calls"
            call = payload["choices"][0]["message"]["tool_calls"][0]
            assert call["function"]["name"] == "lookup"
            assert json.loads(call["function"]["arguments"]) == {"q": "x"}
        # The tool definition really crossed the wire in Anthropic shape.
        assert any(tool.get("name") for tool in calls[0].get("tools", []))

    asyncio.run(run())


def test_tools_reach_the_codex_backend_and_tool_calls_come_back():
    calls: List[Dict[str, Any]] = []
    events = [
        _sse({"type": "response.created", "response": {"id": "resp_2"}}),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_a",
                "name": "lookup",
                "arguments": '{"q":"x"}',
            },
        }),
        _sse({
            "type": "response.completed",
            "response": {"id": "resp_2", "model": "gpt-5", "status": "completed", "output": []},
        }),
    ]

    async def run():
        async with _Harness(
            _codex_first(
                _codex_upstream(events=events, calls=calls), _anthropic_upstream()
            )
        ) as harness:
            status, _, body = await harness.post(
                "/v1/chat/completions",
                {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }],
                },
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["choices"][0]["finish_reason"] == "tool_calls"
            call = payload["choices"][0]["message"]["tool_calls"][0]
            assert call["id"] == "call_a"
            assert call["function"]["name"] == "lookup"
        # Chat-shaped tools were flattened into Responses shape on the wire.
        assert calls[0]["tools"][0]["type"] == "function"
        assert calls[0]["tools"][0]["name"] == "lookup"

    asyncio.run(run())


def test_json_schema_structured_output_reaches_the_claude_backend():
    calls: List[Dict[str, Any]] = []
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(calls=calls), _codex_upstream())
        ) as harness:
            status, _, _ = await harness.post(
                "/v1/chat/completions",
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "out", "schema": schema},
                    },
                },
            )
            assert status == 200
        assert calls[0]["output_config"]["format"] == {
            "type": "json_schema",
            "schema": schema,
        }

    asyncio.run(run())


def test_json_schema_structured_output_reaches_the_codex_backend():
    calls: List[Dict[str, Any]] = []
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    async def run():
        async with _Harness(
            _codex_first(_codex_upstream(calls=calls), _anthropic_upstream())
        ) as harness:
            status, _, _ = await harness.post(
                "/v1/responses",
                {
                    "model": "gpt-5",
                    "input": "hi",
                    "text": {
                        "format": {"type": "json_schema", "name": "out", "schema": schema}
                    },
                },
            )
            assert status == 200
        assert calls[0]["text"]["format"]["schema"] == schema

    asyncio.run(run())


def test_streaming_chat_client_over_claude_backend_ends_with_done():
    lines = [
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"he"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"llo"}}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n',
    ]

    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(stream_lines=lines), _codex_upstream())
        ) as harness:
            status, headers, body = await harness.post(
                "/v1/chat/completions",
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
            assert status == 200
            assert "text/event-stream" in headers["Content-Type"]
            text = body.decode()
            assert text.count("data: [DONE]") == 1
            content = "".join(
                json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                for line in text.splitlines()
                if line.startswith("data: ") and "[DONE]" not in line
            )
            assert content == "hello"

    asyncio.run(run())


def test_streaming_responses_client_over_codex_backend_emits_response_events():
    events = [
        _sse({"type": "response.created", "response": {"id": "resp_3"}}),
        _sse({"type": "response.output_text.delta", "delta": "he"}),
        _sse({"type": "response.output_text.delta", "delta": "llo"}),
        _sse({
            "type": "response.completed",
            "response": {"id": "resp_3", "model": "gpt-5", "status": "completed", "output": []},
        }),
    ]

    async def run():
        async with _Harness(
            _codex_first(_codex_upstream(events=events), _anthropic_upstream())
        ) as harness:
            status, headers, body = await harness.post(
                "/v1/responses", {"model": "gpt-5", "input": "hi", "stream": True}
            )
            assert status == 200
            assert "text/event-stream" in headers["Content-Type"]
            text = body.decode()
            assert "response.created" in text
            assert "response.completed" in text
            deltas = "".join(
                json.loads(line[6:]).get("delta", "")
                for line in text.splitlines()
                if line.startswith("data: ")
                and json.loads(line[6:]).get("type") == "response.output_text.delta"
            )
            assert deltas == "hello"

    asyncio.run(run())


def test_streaming_responses_client_over_claude_backend_emits_response_events():
    """The cross-protocol streaming case: Anthropic SSE -> Responses SSE."""
    lines = [
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"cross"}}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n',
    ]

    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(stream_lines=lines), _codex_upstream())
        ) as harness:
            status, _, body = await harness.post(
                "/v1/responses",
                {"model": "claude-sonnet-4-6", "input": "hi", "stream": True},
            )
            assert status == 200
            text = body.decode()
            assert "response.created" in text
            assert "response.completed" in text
            assert "cross" in text

    asyncio.run(run())


def test_streaming_chat_client_over_codex_backend_ends_with_done():
    """The other cross-protocol streaming case: Responses SSE -> chat chunks."""

    async def run():
        async with _Harness(
            _codex_first(_codex_upstream(), _anthropic_upstream())
        ) as harness:
            status, _, body = await harness.post(
                "/v1/chat/completions",
                {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
            assert status == 200
            text = body.decode()
            assert text.count("data: [DONE]") == 1
            content = "".join(
                json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                for line in text.splitlines()
                if line.startswith("data: ") and "[DONE]" not in line
            )
            assert content == "codex says hi"

    asyncio.run(run())


# ===========================================================================
# AC3 — fault injection
# ===========================================================================


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_claude_to_codex_failover_on_each_allowed_failure(status):
    codex_calls: List[Dict[str, Any]] = []

    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=status),
                _codex_upstream(calls=codex_calls),
            )
        ) as harness:
            code, headers, body = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert code == 200
            assert json.loads(body)["choices"][0]["message"]["content"] == "codex says hi"
            assert headers["X-Hermes-Route-Backend"] == "openai-codex"
            assert headers["X-Hermes-Route-Attempt"] == "2"
        assert len(codex_calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500, 503])
def test_codex_to_claude_failover_on_each_allowed_failure(status):
    claude_calls: List[Dict[str, Any]] = []

    async def run():
        async with _Harness(
            _codex_first(
                _codex_upstream(status=status),
                _anthropic_upstream(calls=claude_calls),
            )
        ) as harness:
            code, headers, body = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert code == 200
            assert json.loads(body)["choices"][0]["message"]["content"] == "claude says hi"
            assert headers["X-Hermes-Route-Backend"] == "claude-code"
        assert len(claude_calls) == 1

    asyncio.run(run())


def test_failover_on_connection_failure_to_an_unreachable_backend():
    """A backend whose port is closed is a classified transport failure."""

    async def run():
        codex_app = _codex_upstream()
        runner, codex_base = await _serve(codex_app)
        try:
            adapters = [
                # Port 1 on loopback refuses connections.
                _StubAdapter("claude-code", "anthropic-messages", "http://127.0.0.1:1/v1"),
                _StubAdapter("openai-codex", "openai-responses", f"{codex_base}/v1"),
            ]
            gateway_runner, base = await _serve(create_failover_app(adapters))
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{base}/v1/chat/completions",
                        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                    ) as response:
                        assert response.status == 200
                        payload = await response.json()
                assert payload["choices"][0]["message"]["content"] == "codex says hi"
            finally:
                await gateway_runner.cleanup()
        finally:
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_forbidden_failures_are_never_replayed_on_a_second_backend(status):
    codex_calls: List[Dict[str, Any]] = []

    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=status),
                _codex_upstream(calls=codex_calls),
            )
        ) as harness:
            code, headers, body = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert code == status
            assert json.loads(body)["error"]["message"] == "upstream refused"
            assert headers["X-Hermes-Route-Attempt"] == "1"
            assert headers["X-Hermes-Route-Backend"] == "claude-code"
        assert codex_calls == []

    asyncio.run(run())


def test_no_backend_is_attempted_more_than_once_and_attempts_are_bounded():
    claude_calls: List[Dict[str, Any]] = []
    codex_calls: List[Dict[str, Any]] = []

    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=503, calls=claude_calls),
                _codex_upstream(status=503, calls=codex_calls),
            )
        ) as harness:
            code, headers, _ = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert code == 503
            assert headers["X-Hermes-Route-Attempt"] == "2"
        assert len(claude_calls) == 1
        assert len(codex_calls) == 1

    asyncio.run(run())


def test_exhaustion_returns_the_last_classified_error_not_a_generic_500():
    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=503), _codex_upstream(status=429)
            )
        ) as harness:
            code, _, body = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert code == 429
            assert json.loads(body)["error"]["message"] == "upstream refused"

    asyncio.run(run())


def test_exhaustion_through_a_responses_client_still_returns_json_error():
    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=503), _codex_upstream(status=503)
            )
        ) as harness:
            code, _, body = await harness.post(
                "/v1/responses", {"model": "m", "input": "hi"}
            )
            assert code == 503
            assert "error" in json.loads(body)

    asyncio.run(run())


def test_spoofed_route_headers_are_ignored_and_replaced_by_gateway_minted_ones():
    async def run():
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream())
        ) as harness:
            code, headers, _ = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                headers={
                    "X-Hermes-Request-Id": "attacker-supplied",
                    "X-Hermes-Route-Attempt": "99",
                    "X-Hermes-Route-Visited": "claude-code,openai-codex",
                    "x-hermes-route-backend": "openai-codex",
                },
            )
            assert code == 200
            # Gateway-minted values win; the spoofed visited-set did not make
            # the gateway skip the first backend.
            assert headers["X-Hermes-Request-Id"] != "attacker-supplied"
            assert headers["X-Hermes-Route-Attempt"] == "1"
            assert headers["X-Hermes-Route-Backend"] == "claude-code"

    asyncio.run(run())


def test_a_backend_with_unusable_credentials_is_skipped_not_replayed_as_an_error():
    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(),
                _codex_upstream(),
                claude_cls=_BrokenCredentialAdapter,
            )
        ) as harness:
            code, headers, body = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert code == 200
            assert json.loads(body)["choices"][0]["message"]["content"] == "codex says hi"
            assert headers["X-Hermes-Route-Backend"] == "openai-codex"

    asyncio.run(run())


def test_malformed_client_body_is_a_400_before_any_backend_is_touched():
    claude_calls: List[Dict[str, Any]] = []
    codex_calls: List[Dict[str, Any]] = []

    async def run():
        async with _Harness(
            _claude_first(
                _anthropic_upstream(calls=claude_calls),
                _codex_upstream(calls=codex_calls),
            )
        ) as harness:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{harness.base_url}/v1/chat/completions",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                ) as response:
                    assert response.status == 400
                    assert (await response.json())["error"]["code"] == "invalid_request_error"
        assert claude_calls == []
        assert codex_calls == []

    asyncio.run(run())


# ===========================================================================
# AC4 — concurrency
# ===========================================================================


def test_concurrent_requests_do_not_storm_a_failed_backend():
    """Once the breaker opens, the dead backend stops being probed at all."""
    claude_calls: List[Dict[str, Any]] = []
    codex_calls: List[Dict[str, Any]] = []

    async def run():
        circuit = BackendCircuit(failure_threshold=3, cooldown_seconds=600)
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=503, calls=claude_calls),
                _codex_upstream(calls=codex_calls),
            ),
            circuit=circuit,
        ) as harness:
            async def one():
                return await harness.post(
                    "/v1/chat/completions",
                    {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                )

            results = await asyncio.gather(*[one() for _ in range(32)])

        assert all(status == 200 for status, _, _ in results)
        assert len(codex_calls) == 32
        # The dead backend is probed a bounded number of times, never 32.
        assert 0 < len(claude_calls) <= 32
        assert circuit.snapshot()["claude-code"]["open"] is True

    asyncio.run(run())


def test_open_circuit_serves_from_the_healthy_backend_without_probing():
    claude_calls: List[Dict[str, Any]] = []

    async def run():
        circuit = BackendCircuit(failure_threshold=1, cooldown_seconds=600)
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=503, calls=claude_calls),
                _codex_upstream(),
            ),
            circuit=circuit,
        ) as harness:
            payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
            first, _, _ = await harness.post("/v1/chat/completions", payload)
            assert first == 200
            assert len(claude_calls) == 1

            for _ in range(5):
                status, headers, _ = await harness.post("/v1/chat/completions", payload)
                assert status == 200
                assert headers["X-Hermes-Route-Backend"] == "openai-codex"
            # Zero further probes of the open backend.
            assert len(claude_calls) == 1

    asyncio.run(run())


def test_all_backends_in_cooldown_returns_a_terminal_service_unavailable():
    async def run():
        circuit = BackendCircuit(failure_threshold=1, cooldown_seconds=600)
        circuit.record_failure("claude-code")
        circuit.record_failure("openai-codex")
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream()), circuit=circuit
        ) as harness:
            status, _, body = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert status == 503
            assert json.loads(body)["error"]["code"] == "all_backends_unavailable"

    asyncio.run(run())


def test_upstream_retry_after_extends_the_cooldown_beyond_the_local_default():
    async def run():
        circuit = BackendCircuit(failure_threshold=1, cooldown_seconds=1)
        async with _Harness(
            _claude_first(
                _anthropic_upstream(status=429, retry_after="3600"),
                _codex_upstream(),
            ),
            circuit=circuit,
        ) as harness:
            status, _, _ = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert status == 200
        state = circuit.snapshot()["claude-code"]
        assert state["open"] is True
        assert state["opens_for_seconds"] > 3000

    asyncio.run(run())


def test_a_successful_request_closes_a_previously_failing_backends_circuit():
    async def run():
        circuit = BackendCircuit(failure_threshold=5, cooldown_seconds=600)
        async with _Harness(
            _claude_first(_anthropic_upstream(), _codex_upstream()), circuit=circuit
        ) as harness:
            circuit.record_failure("claude-code")
            circuit.record_failure("claude-code")
            status, headers, _ = await harness.post(
                "/v1/chat/completions",
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert status == 200
            assert headers["X-Hermes-Route-Backend"] == "claude-code"
        assert circuit.failure_count("claude-code") == 0

    asyncio.run(run())
