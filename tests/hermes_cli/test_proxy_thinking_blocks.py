"""Contracts for Anthropic extended-thinking responses through the Claude proxy.

Claude 4.6+ thinks adaptively by DEFAULT, so an ordinary HTTP 200 routinely
carries ``thinking`` ahead of ``text``.  The translator used to refuse those
blocks, turning valid responses into ``invalid_success_body`` and failing the
request over to a different backend.  The boundary these tests hold:

  * A 200 carrying ``thinking`` / ``redacted_thinking`` alongside ``text`` is a
    VALID Anthropic response and must translate.
  * Reasoning text is recognized but NOT emitted -- it never reaches the
    assistant's ``content``.
  * A genuinely malformed success (unknown block type, malformed text/tool_use,
    bad usage/model/stop_reason) must STILL fail closed, including an unknown
    block type sitting alongside an accepted ``thinking`` block.
  * The non-streaming and streaming paths must agree about which responses are
    acceptable; that asymmetry was the bug's sharpest symptom.
  * Both consumers of the translator -- the failover gateway and the
    single-provider pass-through server -- must agree, since the defect lived in
    the shared module and reached both.

Fixtures are synthetic but their SHAPE is taken from live captures (block type
order ``["thinking", "text"]``, ``stop_reason="end_turn"``).  No captured
prompt, completion, or reasoning text is reproduced here.
"""
import asyncio
import json

import pytest

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.claude_translate import response_to_openai, stream_events
from hermes_cli.proxy.server import create_app

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

TEXT_BLOCK = {"type": "text", "text": "The answer."}
THINKING_BLOCK = {
    "type": "thinking",
    "thinking": "<synthetic reasoning placeholder>",
    "signature": "<synthetic signature placeholder>",
}
REDACTED_BLOCK = {"type": "redacted_thinking", "data": "<synthetic opaque blob>"}
TOOL_USE_BLOCK = {
    "type": "tool_use",
    "id": "toolu_synthetic",
    "name": "mcp__search",
    "input": {"query": "x"},
}


def _message(blocks, **overrides):
    message = {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": blocks,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    message.update(overrides)
    return message


# ---------------------------------------------------------------------------
# Valid extended responses must translate
# ---------------------------------------------------------------------------


def test_thinking_block_response_translates():
    """The exact live shape: adaptive thinking emits ``thinking`` before ``text``."""
    translated = response_to_openai(_message([THINKING_BLOCK, TEXT_BLOCK]))

    message = translated["choices"][0]["message"]
    assert message["content"] == "The answer."
    assert translated["choices"][0]["finish_reason"] == "stop"
    assert translated["usage"]["completion_tokens"] == 20
    # Reasoning text must not leak into the assistant's visible content.
    assert "synthetic reasoning placeholder" not in json.dumps(message)


def test_redacted_thinking_block_response_translates():
    """``redacted_thinking`` is an opaque but legitimate safety-filtered block."""
    translated = response_to_openai(_message([REDACTED_BLOCK, TEXT_BLOCK]))

    assert translated["choices"][0]["message"]["content"] == "The answer."


def test_thinking_only_response_translates():
    """A thinking-only turn is valid; content becomes null, not an error."""
    translated = response_to_openai(_message([THINKING_BLOCK], stop_reason="max_tokens"))

    message = translated["choices"][0]["message"]
    assert message["content"] is None
    assert translated["choices"][0]["finish_reason"] == "length"


def test_thinking_with_tool_use_preserves_tool_calls():
    """Thinking must not disturb tool-call extraction or client tool names."""
    translated = response_to_openai(
        _message([THINKING_BLOCK, TOOL_USE_BLOCK], stop_reason="tool_use"),
        tool_name_map={"mcp__search": "search"},
    )

    choice = translated["choices"][0]
    calls = choice["message"]["tool_calls"]
    assert choice["finish_reason"] == "tool_calls"
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "search"
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "x"}


@pytest.mark.parametrize("reasoning_block", [THINKING_BLOCK, REDACTED_BLOCK])
def test_reasoning_payload_never_reaches_the_translated_response(reasoning_block):
    """Recognized-and-non-emitting means the payload is dropped, not relocated.

    A translator that merely tolerated the block while appending its text would
    satisfy every other test here and still hand the client reasoning it was
    never meant to see, so the whole response is searched, not just ``content``.
    """
    translated = response_to_openai(_message([reasoning_block, TEXT_BLOCK]))

    serialized = json.dumps(translated)
    for secret in reasoning_block.values():
        if secret != reasoning_block["type"]:
            assert secret not in serialized
    assert translated["choices"][0]["message"]["content"] == "The answer."


# ---------------------------------------------------------------------------
# Fail-closed behavior must be preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param([{"type": "server_tool_use", "id": "x"}], id="unknown-block-type"),
        pytest.param([{"type": "text"}], id="text-missing-text"),
        pytest.param([{"type": "text", "text": 5}], id="text-not-a-string"),
        pytest.param([{"type": "tool_use", "id": "", "name": "n", "input": {}}], id="tool-empty-id"),
        pytest.param([{"type": "tool_use", "id": "i", "name": "", "input": {}}], id="tool-empty-name"),
        pytest.param([{"type": "tool_use", "id": "i", "name": "n", "input": []}], id="tool-input-not-object"),
        pytest.param(["not-an-object"], id="block-not-an-object"),
        pytest.param([], id="empty-content"),
    ],
)
def test_malformed_success_still_fails_closed(blocks):
    with pytest.raises(ValueError):
        response_to_openai(_message(blocks))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"usage": {"input_tokens": -1, "output_tokens": 1}}, id="negative-usage"),
        pytest.param({"usage": "nope"}, id="usage-not-an-object"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"stop_reason": 7}, id="stop-reason-not-a-string"),
    ],
)
def test_malformed_envelope_still_fails_closed(overrides):
    with pytest.raises(ValueError):
        response_to_openai(_message([THINKING_BLOCK, TEXT_BLOCK], **overrides))


def test_unknown_block_type_alongside_thinking_still_fails_closed():
    """Accepting thinking must not become 'accept anything unrecognized'."""
    with pytest.raises(ValueError):
        response_to_openai(_message([THINKING_BLOCK, {"type": "future_block"}, TEXT_BLOCK]))


def test_untranslatable_block_error_names_the_offending_type():
    """Telemetry can name what was refused without touching the block's contents."""
    from hermes_cli.proxy.claude_translate import UntranslatableBlockError

    with pytest.raises(UntranslatableBlockError) as excinfo:
        response_to_openai(_message([{"type": "future_block", "secret": "payload"}]))

    assert excinfo.value.block_type == "future_block"
    assert "payload" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Stream / non-stream must agree
# ---------------------------------------------------------------------------


def _sse(events):
    return [b"data: " + json.dumps(event).encode() for event in events]


THINKING_STREAM = _sse([
    {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "<synthetic>"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "The answer."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
])


def _stream_frames(lines):
    frames = []
    for raw in stream_events(lines, "claude-sonnet-5", final=False):
        frames.append(json.loads(raw[6:].strip()))
    return frames


def test_streaming_accepts_thinking_blocks():
    """Streaming skips thinking silently and emits no error frame."""
    frames = _stream_frames(THINKING_STREAM)

    assert not any("error" in frame for frame in frames)
    text = "".join(
        choice.get("delta", {}).get("content", "")
        for frame in frames for choice in frame.get("choices", [])
    )
    assert text == "The answer."
    assert "<synthetic>" not in json.dumps(frames)


def test_streaming_and_nonstreaming_agree_on_thinking():
    """The core asymmetry: one upstream shape, two contradictory verdicts.

    A client that sets ``stream=false`` must not get a 502 for a response the
    same proxy would have served happily as a stream.
    """
    stream_ok = not any("error" in frame for frame in _stream_frames(THINKING_STREAM))

    try:
        response_to_openai(_message([THINKING_BLOCK, TEXT_BLOCK]))
        nonstream_ok = True
    except ValueError:
        nonstream_ok = False

    assert stream_ok == nonstream_ok, (
        "streaming and non-streaming disagree about an identical upstream "
        f"response shape (stream_ok={stream_ok}, nonstream_ok={nonstream_ok})"
    )


def test_streaming_still_reports_malformed_frames():
    """Fail-closed on the streaming side must survive the fix too."""
    frames = _stream_frames(_sse([
        {"type": "content_block_delta", "index": 0, "delta": "not-an-object"},
    ]))

    assert any("error" in frame for frame in frames)


# ---------------------------------------------------------------------------
# The pass-through server is the translator's second consumer
# ---------------------------------------------------------------------------


class _ClaudeBridgeAdapter(UpstreamAdapter):
    """Minimal adapter that routes the pass-through server's Claude bridge."""

    def __init__(self, base_url):
        self._base_url = base_url

    @property
    def name(self):
        return "fake-claude"

    @property
    def display_name(self):
        return "Fake Claude"

    @property
    def allowed_paths(self):
        return frozenset({"/chat/completions"})

    @property
    def transforms_openai_chat(self):
        return True

    def is_authenticated(self):
        return True

    def get_credential(self):
        return UpstreamCredential(
            bearer="synthetic-token",
            base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )


async def _passthrough_roundtrip(upstream_message):
    """POST one non-streaming request through ``server.py:_handle_claude_chat``.

    The pass-through server and the failover gateway are the translator's only
    two call sites; verifying one proves nothing about the other, which is
    exactly how the defect came to live in both.
    """

    async def messages(request):
        await request.read()
        return web.json_response(upstream_message)

    upstream = web.Application()
    upstream.router.add_post("/v1/messages", messages)
    upstream_runner, upstream_base = await _start_app(upstream)
    adapter = _ClaudeBridgeAdapter(f"{upstream_base}/v1")
    proxy_runner, proxy_base = await _start_app(create_app(adapter))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_base}/v1/chat/completions",
                json={
                    "model": "claude-sonnet-5",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ) as response:
                return response.status, json.loads(await response.read())
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()


async def _start_app(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(site._server.sockets)[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def test_passthrough_server_translates_thinking_blocks():
    """The sibling consumer must accept exactly what the gateway accepts."""

    async def run():
        status, body = await _passthrough_roundtrip(_message([THINKING_BLOCK, TEXT_BLOCK]))
        assert status == 200
        assert body["choices"][0]["message"]["content"] == "The answer."
        assert "synthetic reasoning placeholder" not in json.dumps(body)

    asyncio.run(run())


def test_passthrough_server_translates_redacted_thinking_blocks():
    """The sibling accepts Anthropic's opaque safety-filtered thinking block."""

    async def run():
        status, body = await _passthrough_roundtrip(_message([REDACTED_BLOCK, TEXT_BLOCK]))
        assert status == 200
        assert body["choices"][0]["message"]["content"] == "The answer."
        assert "synthetic opaque blob" not in json.dumps(body)

    asyncio.run(run())


def test_passthrough_server_translates_thinking_only_turns():
    """A thinking-only turn yields content null through the sibling too."""

    async def run():
        status, body = await _passthrough_roundtrip(
            _message([THINKING_BLOCK], stop_reason="max_tokens")
        )
        assert status == 200
        assert body["choices"][0]["message"]["content"] is None
        assert body["choices"][0]["finish_reason"] == "length"

    asyncio.run(run())


def test_passthrough_server_preserves_tool_calls_beside_thinking():
    """Recognizing thinking must not disturb an adjacent tool call."""

    async def run():
        status, body = await _passthrough_roundtrip(
            _message([THINKING_BLOCK, TOOL_USE_BLOCK], stop_reason="tool_use")
        )
        assert status == 200
        choice = body["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "mcp__search"
        assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"query": "x"}

    asyncio.run(run())


@pytest.mark.parametrize("blocks", [
    pytest.param([THINKING_BLOCK, {"type": "future_block"}, TEXT_BLOCK], id="unknown-beside-thinking"),
    pytest.param([{"type": "server_tool_use", "id": "x"}], id="unknown-block-type"),
    pytest.param([{"type": "text"}], id="text-missing-text"),
    pytest.param([{"type": "text", "text": 5}], id="text-not-a-string"),
    pytest.param([{"type": "tool_use", "id": "", "name": "n", "input": {}}], id="tool-empty-id"),
    pytest.param([{"type": "tool_use", "id": "i", "name": "", "input": {}}], id="tool-empty-name"),
    pytest.param([{"type": "tool_use", "id": "i", "name": "n", "input": []}], id="tool-input-not-object"),
    pytest.param(["not-an-object"], id="block-not-an-object"),
    pytest.param([], id="empty-content"),
])
def test_passthrough_server_still_fails_closed(blocks):
    """And must refuse exactly what the gateway refuses."""

    async def run():
        status, body = await _passthrough_roundtrip(_message(blocks))
        assert status == 502
        assert body["error"]["code"] == "upstream_invalid_response"

    asyncio.run(run())


@pytest.mark.parametrize("overrides", [
    pytest.param({"usage": {"input_tokens": -1, "output_tokens": 1}}, id="negative-usage"),
    pytest.param({"usage": "nope"}, id="usage-not-an-object"),
    pytest.param({"model": ""}, id="empty-model"),
    pytest.param({"stop_reason": 7}, id="stop-reason-not-a-string"),
])
def test_passthrough_server_rejects_malformed_envelopes(overrides):
    """The sibling carries the parent suite's full envelope reject matrix."""

    async def run():
        status, body = await _passthrough_roundtrip(
            _message([THINKING_BLOCK, TEXT_BLOCK], **overrides)
        )
        assert status == 502
        assert body["error"]["code"] == "upstream_invalid_response"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# invalid_success_body telemetry distinguishes its two causes
# ---------------------------------------------------------------------------


def test_invalid_success_outcome_discriminates_unparseable_from_untranslatable():
    """One label for two causes is what made this defect invisible in the log."""
    from hermes_cli.proxy.legs import _invalid_success_outcome

    unparseable = _invalid_success_outcome()
    untranslatable = _invalid_success_outcome(reason="invalid_success_body:untranslatable")

    assert unparseable.reason == "invalid_success_body:unparseable"
    assert untranslatable.reason == "invalid_success_body:untranslatable"
    # The classification changes; the client-visible outcome does not.
    for outcome in (unparseable, untranslatable):
        assert outcome.ok is False
        assert outcome.status == 502
        assert outcome.failover_eligible is True
        assert outcome.error["error"]["code"] == "upstream_invalid_response"


def test_untranslatable_block_type_is_logged_without_block_contents(caplog):
    """The real leg logs the refused type only -- never the block payload."""

    async def run():
        with caplog.at_level("INFO", logger="hermes_cli.proxy.legs"):
            outcome = await _claude_leg_outcome(
                _message([{"type": "future_block", "secret": "must-not-log"}])
            )
        assert outcome.reason == "invalid_success_body:untranslatable"

    asyncio.run(run())
    assert "untranslatable_block_type=future_block" in caplog.text
    assert "must-not-log" not in caplog.text


async def _claude_leg_outcome(upstream_message):
    """Run the real ``AnthropicMessagesLeg`` against one upstream body.

    Driving the production leg rather than re-deriving its classification is
    what makes the reason string evidence: a test that recomputes the ``except
    ValueError`` branch itself would stay green if the leg stopped using it.
    """
    from hermes_cli.proxy.legs import AnthropicMessagesLeg

    async def messages(request):
        await request.read()
        return web.json_response(upstream_message)

    upstream = web.Application()
    upstream.router.add_post("/v1/messages", messages)
    runner, base = await _start_app(upstream)
    try:
        adapter = _ClaudeBridgeAdapter(f"{base}/v1")
        leg = AnthropicMessagesLeg(adapter)
        return await leg.send(
            adapter.get_credential(),
            {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )
    finally:
        await runner.cleanup()


def test_claude_leg_reports_untranslatable_for_a_refused_block():
    """A parsed-but-refused body is classified apart from a corrupt one."""

    async def run():
        outcome = await _claude_leg_outcome(_message([{"type": "future_block"}]))
        assert outcome.reason == "invalid_success_body:untranslatable"
        assert outcome.failover_eligible is True
        assert outcome.status == 502

    asyncio.run(run())


def test_claude_leg_translates_thinking_instead_of_classifying_it_as_invalid():
    """The live failure: a valid extended-thinking 200 must not be a 502."""

    async def run():
        outcome = await _claude_leg_outcome(_message([THINKING_BLOCK, TEXT_BLOCK]))
        assert outcome.ok is True
        assert outcome.reason == "ok"
        assert outcome.chat["choices"][0]["message"]["content"] == "The answer."
        assert "synthetic reasoning placeholder" not in json.dumps(outcome.chat)

    asyncio.run(run())


def test_claude_leg_reports_unparseable_for_a_non_object_body():
    """The other half of the discriminator, through the same production path."""
    from hermes_cli.proxy.legs import AnthropicMessagesLeg

    async def run():
        async def messages(request):
            await request.read()
            return web.Response(
                body=b"<html>gateway hiccup</html>", status=200, content_type="application/json"
            )

        upstream = web.Application()
        upstream.router.add_post("/v1/messages", messages)
        runner, base = await _start_app(upstream)
        try:
            adapter = _ClaudeBridgeAdapter(f"{base}/v1")
            outcome = await AnthropicMessagesLeg(adapter).send(
                adapter.get_credential(),
                {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
                stream=False,
            )
        finally:
            await runner.cleanup()

        assert outcome.reason == "invalid_success_body:unparseable"
        assert outcome.failover_eligible is True

    asyncio.run(run())
