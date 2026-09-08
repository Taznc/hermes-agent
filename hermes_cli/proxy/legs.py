"""Backend legs: one adapter's wire protocol, spoken for real over HTTP.

Every leg takes a **canonical Chat Completions request** and returns a
:class:`LegOutcome` carrying either a canonical Chat Completions response, a
canonical chunk stream, or a classified failure. That uniformity is what lets
the gateway pick a different backend without knowing anything about the wire
format the previous attempt used.

A leg never writes to the client and never decides policy — it reports what
happened and hands ownership of the upstream connection to the gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover - proxy entry points already guard this
    aiohttp = None  # type: ignore[assignment]

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.routing import is_failover_exception, is_failover_status

logger = logging.getLogger(__name__)

# Wire protocol identifiers an adapter may declare.
WIRE_OPENAI_CHAT = "openai-chat"
WIRE_OPENAI_RESPONSES = "openai-responses"
WIRE_ANTHROPIC_MESSAGES = "anthropic-messages"

_DEFAULT_TIMEOUT_SECONDS = 300.0


async def _noop_close() -> None:
    return None


@dataclass
class LegOutcome:
    """What one backend attempt produced, in canonical form."""

    ok: bool
    status: int
    #: Canonical Chat Completions response (non-streaming success).
    chat: Optional[Dict[str, Any]] = None
    #: Canonical ``chat.completion.chunk`` stream (streaming success). Reading
    #: it commits the response, so the gateway must not fail over afterwards.
    chunks: Optional[AsyncIterator[Dict[str, Any]]] = None
    #: Async cleanup for the upstream connection this outcome owns.
    close: Callable[[], Awaitable[None]] = _noop_close
    #: Upstream error body, already canonicalized to an OpenAI error object.
    error: Optional[Dict[str, Any]] = None
    #: Whether policy permits retrying this request on a different backend.
    failover_eligible: bool = False
    #: Upstream-declared reset deadline, seconds (from ``Retry-After``).
    retry_after: Optional[float] = None
    #: Short machine-readable reason for telemetry; never carries user data.
    reason: str = ""
    headers: Dict[str, str] = field(default_factory=dict)


def _error_payload(message: str, code: str) -> Dict[str, Any]:
    return {"error": {"message": message, "type": code, "code": code}}


def _parse_retry_after(headers: Any) -> Optional[float]:
    """Read a numeric ``Retry-After`` if the upstream sent one."""
    try:
        raw = headers.get("Retry-After")
    except Exception:  # pragma: no cover - defensive; headers are mapping-like
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        # HTTP-date form; the local cooldown covers it rather than guessing.
        return None
    return value if value >= 0 else None


class BackendLeg:
    """Speak one backend's wire protocol on behalf of the gateway."""

    def __init__(self, adapter: UpstreamAdapter) -> None:
        self.adapter = adapter

    @property
    def name(self) -> str:
        return self.adapter.name

    async def send(
        self,
        credential: UpstreamCredential,
        chat_request: Dict[str, Any],
        *,
        stream: bool,
    ) -> LegOutcome:
        raise NotImplementedError

    # -- shared plumbing ---------------------------------------------------

    async def _post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Dict[str, str],
    ):
        """POST to the upstream, converting classified transport faults."""
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=15, sock_read=_DEFAULT_TIMEOUT_SECONDS
        )
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            response = await session.post(url, data=body, headers=headers)
        except asyncio.CancelledError:
            await session.close()
            raise
        except Exception as exc:
            await session.close()
            if is_failover_exception(exc):
                return None, LegOutcome(
                    ok=False,
                    status=502,
                    error=_error_payload(
                        f"upstream connection failed: {exc}", "upstream_unreachable"
                    ),
                    failover_eligible=True,
                    reason="transport_error",
                )
            raise
        return (session, response), None

    async def _classify_error_response(self, session, response) -> LegOutcome:
        """Turn a non-2xx upstream response into a classified outcome."""
        try:
            raw = await response.read()
        finally:
            response.release()
            await session.close()
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if not isinstance(parsed, dict) or "error" not in parsed:
            detail = ""
            if isinstance(parsed, dict):
                # Preserve whatever the upstream did say; an opaque
                # "HTTP 400" is unactionable for the operator.
                detail = f": {json.dumps(parsed)[:400]}"
            elif raw:
                detail = f": {raw[:400].decode('utf-8', 'replace')}"
            parsed = _error_payload(
                f"upstream returned HTTP {response.status}{detail}", "upstream_error"
            )
        eligible = is_failover_status(response.status)
        return LegOutcome(
            ok=False,
            status=response.status,
            error=parsed,
            failover_eligible=eligible,
            retry_after=_parse_retry_after(response.headers),
            reason=f"http_{response.status}",
        )


class OpenAIChatLeg(BackendLeg):
    """A backend that already speaks OpenAI Chat Completions natively."""

    async def send(self, credential, chat_request, *, stream):
        payload = dict(chat_request)
        payload["stream"] = bool(stream)
        opened, failure = await self._post(
            f"{credential.base_url.rstrip('/')}/chat/completions",
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=self._headers(credential),
        )
        if failure is not None:
            return failure
        session, response = opened
        if response.status >= 400:
            return await self._classify_error_response(session, response)
        if stream:
            return LegOutcome(
                ok=True,
                status=response.status,
                chunks=_passthrough_chunks(response),
                close=_closer(session, response),
                reason="ok_stream",
            )
        try:
            raw = await response.read()
        finally:
            response.release()
            await session.close()
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            return LegOutcome(
                ok=False,
                status=502,
                error=_error_payload(
                    "upstream returned a success status with an unusable body",
                    "upstream_invalid_response",
                ),
                failover_eligible=True,
                reason="invalid_success_body",
            )
        return LegOutcome(ok=True, status=response.status, chat=parsed, reason="ok")

    def _headers(self, credential: UpstreamCredential) -> Dict[str, str]:
        headers = {
            "Authorization": f"{credential.token_type} {credential.bearer}",
            "Content-Type": "application/json",
        }
        headers.update(self.adapter.get_upstream_headers(credential))
        return headers


class AnthropicMessagesLeg(BackendLeg):
    """The Claude subscription leg, reusing the reviewed Anthropic bridge."""

    async def send(self, credential, chat_request, *, stream):
        from hermes_cli.proxy.claude_translate import (
            ClaudeStreamTranslator,
            prepare_chat_request,
            response_to_openai,
        )

        payload = dict(chat_request)
        payload["stream"] = bool(stream)
        try:
            headers, body, tool_name_map = prepare_chat_request(payload)
        except ValueError as exc:
            # The request itself is unusable, so a second backend would reject
            # it identically; this is terminal by policy, not a failover.
            return LegOutcome(
                ok=False,
                status=400,
                error=_error_payload(
                    f"Invalid chat completion request: {exc}", "invalid_request_error"
                ),
                reason="invalid_request",
            )
        headers["Authorization"] = f"{credential.token_type} {credential.bearer}"
        headers["Content-Type"] = "application/json"

        opened, failure = await self._post(
            f"{credential.base_url.rstrip('/')}/messages", body=body, headers=headers
        )
        if failure is not None:
            return failure
        session, response = opened
        if response.status >= 400:
            return await self._classify_error_response(session, response)

        if stream:
            model = str(chat_request.get("model") or "claude")
            translator = ClaudeStreamTranslator(model, tool_name_map=tool_name_map)

            async def chunks() -> AsyncIterator[Dict[str, Any]]:
                async for line in response.content:
                    for frame in translator.translate(line):
                        # ClaudeStreamTranslator emits encoded SSE frames; the
                        # gateway's canonical currency is the chunk object.
                        if frame.startswith(b"data: "):
                            try:
                                yield json.loads(frame[6:].strip())
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                continue

            return LegOutcome(
                ok=True,
                status=response.status,
                chunks=chunks(),
                close=_closer(session, response),
                reason="ok_stream",
            )

        try:
            raw = await response.read()
        finally:
            response.release()
            await session.close()
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            return _invalid_success_outcome()
        try:
            return LegOutcome(
                ok=True,
                status=response.status,
                chat=response_to_openai(parsed, tool_name_map=tool_name_map),
                reason="ok",
            )
        except ValueError:
            return _invalid_success_outcome()


class OpenAIResponsesLeg(BackendLeg):
    """The Codex subscription leg: canonical chat in, Responses on the wire."""

    async def send(self, credential, chat_request, *, stream):
        from hermes_cli.proxy.responses_translate import (
            ResponsesStreamTranslator,
            chat_request_to_responses,
            responses_response_to_chat,
        )

        try:
            payload = chat_request_to_responses(chat_request)
        except ValueError as exc:
            return LegOutcome(
                ok=False,
                status=400,
                error=_error_payload(
                    f"Invalid chat completion request: {exc}", "invalid_request_error"
                ),
                reason="invalid_request",
            )
        for unsupported in self.adapter.unsupported_responses_params:
            payload.pop(unsupported, None)
        # Codex accepts only streamed Responses requests; a non-streaming
        # client is served by materializing the terminal object below.
        payload["stream"] = True
        # The ChatGPT-subscription endpoint refuses a stored response outright
        # ({"detail":"Store must be set to false"}), so the leg states it rather
        # than depending on the client to know a backend-specific requirement.
        payload["store"] = False

        headers = {
            "Authorization": f"{credential.token_type} {credential.bearer}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        headers.update(self.adapter.get_upstream_headers(credential))

        opened, failure = await self._post(
            f"{credential.base_url.rstrip('/')}/responses",
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
        )
        if failure is not None:
            return failure
        session, response = opened
        if response.status >= 400:
            return await self._classify_error_response(session, response)

        model = str(chat_request.get("model") or "")
        if stream:
            translator = ResponsesStreamTranslator(model)

            async def chunks() -> AsyncIterator[Dict[str, Any]]:
                async for line in response.content:
                    for chunk in translator.translate(line):
                        yield chunk

            return LegOutcome(
                ok=True,
                status=response.status,
                chunks=chunks(),
                close=_closer(session, response),
                reason="ok_stream",
            )

        try:
            raw = await response.read()
        finally:
            response.release()
            await session.close()

        terminal = _terminal_response_object(raw)
        if terminal is None:
            return LegOutcome(
                ok=False,
                status=502,
                error=_error_payload(
                    "upstream stream ended without a response.completed event",
                    "upstream_incomplete_response",
                ),
                failover_eligible=True,
                reason="incomplete_stream",
            )
        try:
            return LegOutcome(
                ok=True,
                status=response.status,
                chat=responses_response_to_chat(terminal),
                reason="ok",
            )
        except ValueError:
            return _invalid_success_outcome()


def _invalid_success_outcome() -> LegOutcome:
    """A 2xx whose body cannot be translated is an upstream defect, not a refusal.

    It is failover-eligible: a second subscription may well answer correctly,
    and the client has been told nothing yet.
    """
    return LegOutcome(
        ok=False,
        status=502,
        error=_error_payload(
            "upstream returned a success status with an unusable body",
            "upstream_invalid_response",
        ),
        failover_eligible=True,
        reason="invalid_success_body",
    )


def _terminal_response_object(raw: bytes) -> Optional[Dict[str, Any]]:
    """Extract the terminal Responses object from a complete SSE body."""
    output_items: Dict[str, Dict[str, Any]] = {}
    for frame in raw.replace(b"\r\n", b"\n").split(b"\n\n"):
        data_lines = [
            line[5:].strip() for line in frame.splitlines() if line.startswith(b"data:")
        ]
        if not data_lines:
            continue
        try:
            event = json.loads(b"\n".join(data_lines).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                output_items[str(item.get("id") or len(output_items))] = item
        if event.get("type") == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                response = dict(response)
                if output_items:
                    response["output"] = list(output_items.values())
                return response
    return None


async def _passthrough_chunks(response) -> AsyncIterator[Dict[str, Any]]:
    """Decode an OpenAI SSE stream back into canonical chunk objects."""
    async for line in response.content:
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(chunk, dict):
            yield chunk


def _closer(session, response) -> Callable[[], Awaitable[None]]:
    async def close() -> None:
        response.release()
        await session.close()

    return close


_LEGS = {
    WIRE_OPENAI_CHAT: OpenAIChatLeg,
    WIRE_OPENAI_RESPONSES: OpenAIResponsesLeg,
    WIRE_ANTHROPIC_MESSAGES: AnthropicMessagesLeg,
}


def resolve_leg(adapter: UpstreamAdapter) -> BackendLeg:
    """Build the leg for an adapter's declared wire protocol."""
    wire = getattr(adapter, "wire_protocol", WIRE_OPENAI_CHAT)
    leg = _LEGS.get(str(wire))
    if leg is None:
        raise ValueError(
            f"Adapter {adapter.name!r} declares unknown wire protocol {wire!r}. "
            f"Known: {', '.join(sorted(_LEGS))}."
        )
    return leg(adapter)


__all__ = [
    "WIRE_ANTHROPIC_MESSAGES",
    "WIRE_OPENAI_CHAT",
    "WIRE_OPENAI_RESPONSES",
    "AnthropicMessagesLeg",
    "BackendLeg",
    "LegOutcome",
    "OpenAIChatLeg",
    "OpenAIResponsesLeg",
    "resolve_leg",
]
