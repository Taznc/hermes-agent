"""One client-facing ingress that fails over across subscription backends.

Topology: the gateway is the only thing a client talks to. Backends are private
adapters invoked **directly** through :mod:`hermes_cli.proxy.legs` — never by
calling the gateway's own URL — which is what makes the anti-loop contract
structural rather than a convention.

Protocol: the gateway normalizes every request to Chat Completions, sends that
to whichever backend it selects, and re-encodes the answer into the client's own
API family. A Responses client can therefore be served by the Anthropic backend
and a Chat Completions client by the Codex backend.

Single-provider behaviour is unchanged: ``create_app`` in ``server.py`` still
owns the credential-attaching pass-through, and this module is only reached when
two or more backends are configured.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Dict, List, Optional, Sequence

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - proxy entry points already guard this
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import UpstreamAdapter
from hermes_cli.proxy.legs import LegOutcome, resolve_leg
from hermes_cli.proxy.routing import (
    INTERNAL_ROUTE_HEADERS,
    REQUEST_ID_HEADER,
    ROUTE_ATTEMPT_HEADER,
    ROUTE_BACKEND_HEADER,
    BackendCircuit,
    RouteContext,
)

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 10_000_000

CLIENT_API_CHAT = "chat"
CLIENT_API_RESPONSES = "responses"


def _json_error(status: int, message: str, code: str) -> "web.Response":
    return web.json_response(
        {"error": {"message": message, "type": code, "code": code}}, status=status
    )


def _spoofed_route_headers(request: "web.Request") -> List[str]:
    """Names of gateway-owned routing headers the client tried to supply.

    Inbound headers are never consulted for routing and are never forwarded —
    :mod:`hermes_cli.proxy.legs` builds each backend's header set from scratch —
    so a spoofed value cannot influence selection. This returns the names purely
    so the attempt is observable in telemetry.
    """
    return [
        str(name)
        for name in request.headers
        if str(name).lower() in INTERNAL_ROUTE_HEADERS
    ]


def create_failover_app(
    adapters: Sequence[UpstreamAdapter],
    *,
    client_auth_token: Optional[str] = None,
    circuit: Optional[BackendCircuit] = None,
) -> "web.Application":
    """Build the multi-backend failover ingress.

    ``adapters`` is the ordered preference list; the first healthy backend that
    has not been visited for this request wins.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )
    backends = list(adapters)
    if len(backends) < 2:
        raise ValueError(
            "The failover gateway requires at least two backends; "
            "use create_app() for single-provider pass-through."
        )
    names = [adapter.name for adapter in backends]
    if len(set(names)) != len(names):
        raise ValueError(f"Duplicate proxy backends configured: {names}")
    if any(adapter.requires_client_auth for adapter in backends) and not client_auth_token:
        raise RuntimeError(
            "This backend set requires client authentication; "
            "start the proxy with --auth-token-file."
        )

    legs = {adapter.name: resolve_leg(adapter) for adapter in backends}
    breaker = circuit if circuit is not None else BackendCircuit()

    app = web.Application(client_max_size=MAX_REQUEST_BYTES)

    def _authorized(request: "web.Request") -> bool:
        if client_auth_token is None:
            return True
        scheme, separator, supplied = request.headers.get(
            "Authorization", ""
        ).partition(" ")
        supplied = supplied.strip()
        if not separator or scheme.lower() != "bearer" or not supplied:
            return False
        return hmac.compare_digest(
            supplied.encode("utf-8"), client_auth_token.encode("utf-8")
        )

    async def handle_health(request: "web.Request") -> "web.Response":
        if not _authorized(request):
            return _json_error(
                401, "A valid proxy bearer token is required.", "proxy_auth_failed"
            )
        backend_states = []
        for adapter in backends:
            backend_states.append({
                "name": adapter.name,
                "display_name": adapter.display_name,
                "authenticated": await asyncio.to_thread(adapter.is_authenticated),
            })
        return web.json_response({
            "status": "ok",
            "mode": "failover",
            "backends": backend_states,
            "circuits": breaker.snapshot(),
        })

    async def _dispatch(
        request: "web.Request", client_api: str
    ) -> "web.StreamResponse":
        if not _authorized(request):
            return _json_error(
                401, "A valid proxy bearer token is required.", "proxy_auth_failed"
            )
        spoofed = _spoofed_route_headers(request)
        if spoofed:
            logger.warning(
                "proxy: ignoring client-supplied route headers: %s",
                ", ".join(sorted(spoofed)),
            )
        raw = await request.read()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            chat_request = _to_canonical(payload, client_api)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            return _json_error(
                400, f"Invalid request: {exc}", "invalid_request_error"
            )

        wants_stream = bool(payload.get("stream"))
        context = RouteContext.mint(max_attempts=len(backends))
        last: Optional[LegOutcome] = None
        skipped: List[str] = []

        # Selection is driven by the route context, not by iterating the
        # backend list: each pass asks for the next backend the contract still
        # permits. That is what makes "at most once per backend, bounded total
        # attempts" a property of the loop rather than of its shape.
        while True:
            adapter = next(
                (
                    candidate
                    for candidate in backends
                    if context.may_attempt(candidate.name)
                ),
                None,
            )
            if adapter is None:
                break
            name = adapter.name
            if not breaker.allows(name):
                skipped.append(name)
                context.record_attempt(name)
                continue
            try:
                credential = await asyncio.to_thread(adapter.get_credential)
            except Exception as exc:
                # An unusable credential is this backend's problem, not the
                # client's request; move on rather than replaying an auth error.
                logger.warning(
                    "proxy: backend %s credential resolution failed: %s", name, exc
                )
                breaker.record_failure(name)
                context.record_attempt(name)
                last = LegOutcome(
                    ok=False,
                    status=401,
                    error={
                        "error": {
                            "message": str(exc),
                            "type": "upstream_auth_failed",
                            "code": "upstream_auth_failed",
                        }
                    },
                    failover_eligible=True,
                    reason="credential_error",
                )
                continue

            context.record_attempt(name)
            outcome = await legs[name].send(
                credential, chat_request, stream=wants_stream
            )
            _log_attempt(context, name, outcome)

            if outcome.ok:
                breaker.record_success(name)
                return await _emit(
                    request, outcome, client_api, context, name, chat_request
                )

            breaker.record_failure(name, retry_after_seconds=outcome.retry_after)
            last = outcome
            if not outcome.failover_eligible:
                # Terminal by policy: replaying a validation/auth/config error
                # only spends a second subscription's quota on the same refusal.
                return _terminal(outcome, client_api, context, name)

        if last is None:
            return _json_error(
                503,
                "All configured backends are in cooldown; no attempt was made. "
                f"Skipped: {', '.join(skipped) or 'none'}.",
                "all_backends_unavailable",
            )
        return _terminal(last, client_api, context, context_backend(context, backends))

    async def handle_chat(request: "web.Request") -> "web.StreamResponse":
        return await _dispatch(request, CLIENT_API_CHAT)

    async def handle_responses(request: "web.Request") -> "web.StreamResponse":
        return await _dispatch(request, CLIENT_API_RESPONSES)

    async def handle_unknown(request: "web.Request") -> "web.Response":
        return _json_error(
            404,
            f"Path {request.path} is not served by the failover gateway. "
            "Available: /v1/chat/completions, /v1/responses.",
            "path_not_allowed",
        )

    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_route("*", "/v1/{tail:.*}", handle_unknown)

    async def _emit(
        request: "web.Request",
        outcome: LegOutcome,
        client_api: str,
        context: RouteContext,
        backend: str,
        chat_request: Dict[str, Any],
    ) -> "web.StreamResponse":
        headers = _route_headers(context, backend)
        if outcome.chunks is None:
            try:
                body = _from_canonical(outcome.chat or {}, client_api)
            except ValueError as exc:
                await outcome.close()
                return _json_error(
                    502,
                    f"backend response could not be translated: {exc}",
                    "upstream_invalid_response",
                )
            await outcome.close()
            return web.json_response(body, status=200, headers=headers)

        # Streaming: once a byte is written the response is committed, so no
        # failover is possible past this point. That is also what makes a retry
        # safe for a storage client — a failed-over request never produced a
        # partial response the client could have persisted.
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                **headers,
            },
        )
        try:
            await response.prepare(request)
            if client_api == CLIENT_API_CHAT:
                async for chunk in outcome.chunks:
                    await response.write(
                        b"data: "
                        + json.dumps(chunk, separators=(",", ":")).encode()
                        + b"\n\n"
                    )
                await response.write(b"data: [DONE]\n\n")
            else:
                from hermes_cli.proxy.responses_translate import (
                    chat_chunk_to_responses_events,
                )

                state: Dict[str, Any] = {}
                async for chunk in outcome.chunks:
                    for frame in chat_chunk_to_responses_events(chunk, state):
                        await response.write(frame)
                if not state.get("completed"):
                    # The upstream ended without a terminal event; synthesize
                    # one so a Responses client is never left waiting.
                    for frame in chat_chunk_to_responses_events(
                        {
                            "model": chat_request.get("model") or "",
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        },
                        state,
                    ):
                        await response.write(frame)
            await response.write_eof()
            return response
        finally:
            await outcome.close()

    return app


def context_backend(context: RouteContext, backends: Sequence[UpstreamAdapter]) -> str:
    """The last backend this request actually attempted."""
    for adapter in reversed(list(backends)):
        if adapter.name in context.visited:
            return adapter.name
    return ""


def _route_headers(context: RouteContext, backend: str) -> Dict[str, str]:
    return {
        REQUEST_ID_HEADER: context.request_id,
        ROUTE_BACKEND_HEADER: backend,
        ROUTE_ATTEMPT_HEADER: str(context.attempts),
    }


def _terminal(
    outcome: LegOutcome, client_api: str, context: RouteContext, backend: str
) -> "web.Response":
    """Return the final classified error; selection is never restarted."""
    body = outcome.error or {
        "error": {
            "message": "upstream request failed",
            "type": "upstream_error",
            "code": "upstream_error",
        }
    }
    return web.json_response(
        body, status=outcome.status, headers=_route_headers(context, backend)
    )


def _to_canonical(payload: Dict[str, Any], client_api: str) -> Dict[str, Any]:
    if client_api == CLIENT_API_CHAT:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("chat completion requires a non-empty messages array")
        return payload
    from hermes_cli.proxy.responses_translate import responses_request_to_chat

    return responses_request_to_chat(payload)


def _from_canonical(chat: Dict[str, Any], client_api: str) -> Dict[str, Any]:
    if client_api == CLIENT_API_CHAT:
        return chat
    from hermes_cli.proxy.responses_translate import chat_response_to_responses

    return chat_response_to_responses(chat)


def _log_attempt(context: RouteContext, backend: str, outcome: LegOutcome) -> None:
    """Structured route telemetry: identifiers and classifications only.

    Deliberately carries no prompt, token, account id, or response body — the
    fields below are all gateway-minted or fixed classification labels.
    """
    logger.info(
        "proxy.route request_id=%s attempt=%d backend=%s status=%d ok=%s "
        "reason=%s failover_eligible=%s",
        context.request_id,
        context.attempts,
        backend,
        outcome.status,
        outcome.ok,
        outcome.reason,
        outcome.failover_eligible,
    )


__all__ = ["create_failover_app", "CLIENT_API_CHAT", "CLIENT_API_RESPONSES"]
