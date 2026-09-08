"""HTTP server that forwards OpenAI-compatible requests to a configured upstream.

Listens on ``http://<host>:<port>/v1/<path>`` and forwards each request to
``<upstream-base-url>/<path>`` with the client's ``Authorization`` header
replaced by a freshly-resolved bearer from the configured adapter. The
response is streamed back unmodified, preserving SSE.

The server is intentionally minimal: it does NOT mediate, log, transform,
or rewrite request/response bodies. It's a credential-attaching forwarder.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import signal
from typing import Optional

try:
    import aiohttp
    from aiohttp import web
    from yarl import URL

    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    URL = None  # type: ignore[assignment,misc]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

# Headers we strip when forwarding to the upstream. ``host``/``content-length``
# are recomputed by aiohttp; ``authorization`` is replaced with our bearer.
# Everything else (content-type, accept, user-agent, x-* headers) passes through.
_HOP_BY_HOP_HEADERS = frozenset({
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "authorization",  # we replace this one
})

DEFAULT_PORT = 8645
DEFAULT_HOST = "127.0.0.1"
# Body cap for forwarded requests. Chat-completion payloads with long agent
# conversations can be large; mirror api_server's MAX_REQUEST_BYTES (10 MB).
# client_max_size bounds every read path, including chunked bodies.
MAX_REQUEST_BYTES = 10_000_000


def is_loopback_host(host: str) -> bool:
    """True only for explicit loopback literals or localhost."""
    value = str(host or "").strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _json_error(status: int, message: str, code: str = "proxy_error") -> "web.Response":
    """Return an OpenAI-style error JSON response."""
    body = {"error": {"message": message, "type": code, "code": code}}
    return web.json_response(body, status=status)


def _filter_request_headers(headers: "aiohttp.typedefs.LooseHeaders") -> dict:
    """Strip hop-by-hop + auth headers from the inbound request."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


def _strip_owned_headers(headers: dict, owned_names: frozenset[str]) -> dict:
    """Remove all client spellings of adapter-owned identity headers."""
    owned = {str(name).strip().lower() for name in owned_names if str(name).strip()}
    return {
        key: value for key, value in headers.items() if str(key).lower() not in owned
    }


def _merge_adapter_headers(
    headers: dict,
    adapter_headers: dict[str, str],
) -> dict:
    """Overlay trusted adapter headers case-insensitively.

    Client-controlled values with alternate casing must not survive alongside
    Codex account/originator headers. Authorization remains owned exclusively
    by the server's credential path.
    """
    merged = dict(headers)
    for key, value in adapter_headers.items():
        normalized = str(key).strip()
        if not normalized or normalized.lower() in _HOP_BY_HOP_HEADERS:
            continue
        for existing in list(merged):
            if str(existing).lower() == normalized.lower():
                merged.pop(existing, None)
        merged[normalized] = str(value)
    return merged


def _filter_response_headers(headers) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        # aiohttp recomputes Content-Encoding/Content-Length on stream — let it.
        if key.lower() in {"content-encoding", "content-length"}:
            continue
        out[key] = value
    return out


async def _open_upstream_request(
    *,
    method: str,
    url,
    body: bytes,
    headers: dict,
    timeout,
):
    """Open one upstream request and close its session on every failed setup."""
    try:
        session = aiohttp.ClientSession(timeout=timeout)
    except Exception as exc:  # pragma: no cover - aiohttp setup issue
        raise RuntimeError(f"proxy session init failed: {exc}") from exc

    try:
        response = await session.request(
            method,
            url,
            data=body if body else None,
            headers=headers,
            allow_redirects=False,
        )
    except asyncio.CancelledError:
        await session.close()
        raise
    except Exception:
        await session.close()
        raise
    return session, response


async def _handle_claude_chat(request: "web.Request", cred: UpstreamCredential, body: bytes) -> "web.StreamResponse":
    """Run the non-passthrough Claude Code wire bridge without logging its body."""
    from hermes_cli.proxy.claude_translate import (
        ClaudeStreamTranslator,
        prepare_chat_request,
        response_to_openai,
    )
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        headers, outbound, tool_name_map = prepare_chat_request(payload)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return _json_error(400, f"Invalid OpenAI chat completion request: {exc}", code="invalid_request_error")
    headers["Authorization"] = f"{cred.token_type} {cred.bearer}"
    headers["Content-Type"] = "application/json"
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300))
    try:
        upstream = await session.post(f"{cred.base_url.rstrip('/')}/messages", data=outbound, headers=headers)
    except asyncio.CancelledError:
        await session.close()
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        await session.close()
        return _json_error(502, f"upstream connection failed: {exc}", code="upstream_unreachable")
    except Exception:
        await session.close()
        raise
    if not payload.get("stream"):
        try:
            body_bytes = await upstream.read()
            try:
                raw = json.loads(body_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raw = None
            if upstream.status >= 400:
                # A genuine refusal keeps its status; an undecodable error body
                # still reaches the client as JSON rather than aiohttp's 500.
                if raw is None:
                    return _json_error(
                        upstream.status,
                        "upstream returned a non-JSON error response",
                        code="upstream_error",
                    )
                return web.json_response(raw, status=upstream.status)
            if not isinstance(raw, dict):
                # HTTP 200 that is not an Anthropic message object cannot be
                # translated; surfacing it as success hands the client prose it
                # will fail to parse with no indication the upstream misbehaved.
                return _json_error(
                    502,
                    "upstream returned a success status with an unusable body",
                    code="upstream_invalid_response",
                )
            return web.json_response(response_to_openai(raw, tool_name_map=tool_name_map), status=upstream.status)
        finally:
            upstream.release()
            await session.close()
    response = web.StreamResponse(status=upstream.status, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    try:
        await response.prepare(request)
        if upstream.status >= 400:
            await response.write(b"data: " + await upstream.read() + b"\n\n")
        else:
            model = str(payload.get("model") or "claude")
            translator = ClaudeStreamTranslator(model, tool_name_map=tool_name_map)
            async for line in upstream.content:
                for frame in translator.translate(line):
                    await response.write(frame)
            await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response
    finally:
        upstream.release()
        await session.close()


async def _stream_upstream_response(
    request: "web.Request",
    upstream_resp,
    session,
) -> "web.StreamResponse":
    """Bridge one upstream response and always release transport resources."""
    resp = web.StreamResponse(
        status=upstream_resp.status,
        headers=_filter_response_headers(upstream_resp.headers),
    )
    try:
        # Cleanup ownership starts before prepare: downstream disconnects while
        # sending headers must still release the already-open upstream response.
        await resp.prepare(request)
        try:
            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await resp.write(chunk)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(
                "proxy: upstream stream interrupted; aborting downstream: %s",
                exc,
            )
            transport = request.transport
            if transport is not None:
                transport.abort()
            raise
        await resp.write_eof()
        return resp
    finally:
        upstream_resp.release()
        await session.close()


async def _materialize_responses_stream(upstream_resp, session) -> "web.Response":
    """Return the terminal OpenAI Response from an SSE-only upstream.

    Codex's Responses API requires ``stream: true``. Hindsight uses the
    compatible non-streaming API, so the proxy consumes the upstream event
    stream and returns its terminal ``response.completed`` object as JSON.
    """
    try:
        raw = await upstream_resp.read()
        if upstream_resp.status >= 400:
            return web.Response(
                body=raw,
                status=upstream_resp.status,
                headers=_filter_response_headers(upstream_resp.headers),
            )

        output_items: dict[str, dict] = {}
        for frame in raw.replace(b"\r\n", b"\n").split(b"\n\n"):
            data_lines = [
                line[5:].strip()
                for line in frame.splitlines()
                if line.startswith(b"data:")
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
                    item_id = str(item.get("id") or len(output_items))
                    output_items[item_id] = item
            if event.get("type") == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    response = dict(response)
                    if output_items:
                        response["output"] = list(output_items.values())
                    headers = _filter_response_headers(upstream_resp.headers)
                    headers.pop("Content-Type", None)
                    headers.pop("content-type", None)
                    return web.json_response(response, status=upstream_resp.status, headers=headers)

        return _json_error(
            502,
            "Codex stream ended without a response.completed event.",
            code="upstream_incomplete_response",
        )
    finally:
        upstream_resp.release()
        await session.close()


def create_app(
    adapter: UpstreamAdapter,
    *,
    client_auth_token: Optional[str] = None,
) -> "web.Application":
    """Build the aiohttp application bound to a specific upstream adapter."""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )
    if adapter.requires_client_auth and not client_auth_token:
        raise RuntimeError(
            f"{adapter.display_name} proxy requires client authentication."
        )

    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    # AppKey ensures forward-compat with future aiohttp versions that strip
    # bare-string keys.
    _adapter_key = web.AppKey("adapter", UpstreamAdapter)
    app[_adapter_key] = adapter

    def _client_is_authorized(request: "web.Request") -> bool:
        if client_auth_token is None:
            return True
        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        supplied = supplied.strip()
        if not separator or scheme.lower() != "bearer" or not supplied:
            return False
        return hmac.compare_digest(
            supplied.encode("utf-8"),
            client_auth_token.encode("utf-8"),
        )

    def _client_auth_error() -> "web.Response":
        return _json_error(
            401,
            "A valid proxy bearer token is required.",
            code="proxy_auth_failed",
        )

    async def handle_health(request: "web.Request") -> "web.Response":
        if not _client_is_authorized(request):
            return _client_auth_error()
        return web.json_response({
            "status": "ok",
            "upstream": adapter.display_name,
            "authenticated": await asyncio.to_thread(adapter.is_authenticated),
        })

    async def handle_proxy(request: "web.Request") -> "web.StreamResponse":
        if not _client_is_authorized(request):
            return _client_auth_error()

        # Extract the path *after* /v1
        rel_path = request.match_info.get("tail", "")
        rel_path = "/" + rel_path.lstrip("/")

        if rel_path not in adapter.allowed_paths:
            allowed = ", ".join(sorted(adapter.allowed_paths))
            return _json_error(
                404,
                f"Path /v1{rel_path} is not forwarded by this proxy. "
                f"Allowed: {allowed}",
                code="path_not_allowed",
            )

        try:
            cred = await asyncio.to_thread(adapter.get_credential)
        except Exception as exc:
            logger.warning("proxy: credential resolution failed: %s", exc)
            return _json_error(401, str(exc), code="upstream_auth_failed")

        # Forward body verbatim. Read into memory once — request bodies for
        # chat/completions/embeddings are small (<1MB typically). If we ever
        # need to forward large multipart uploads we'll switch to streaming
        # the request body too.
        body = await request.read()
        materialize_responses_stream = False
        if rel_path == "/responses":
            needs_stream = bool(
                getattr(adapter, "materializes_responses_stream", False)
            )
            drop_params = frozenset(
                getattr(adapter, "unsupported_responses_params", frozenset())
            )
            if needs_stream or drop_params:
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    rewritten = False
                    # Never log payload contents; only the parameter names,
                    # which are fixed adapter metadata rather than user data.
                    dropped = sorted(drop_params & payload.keys())
                    for key in dropped:
                        payload.pop(key, None)
                        rewritten = True
                    if dropped:
                        logger.debug(
                            "proxy: dropped upstream-unsupported /responses params: %s",
                            ", ".join(dropped),
                        )
                    if needs_stream and not payload.get("stream"):
                        payload["stream"] = True
                        materialize_responses_stream = True
                        rewritten = True
                    if rewritten:
                        body = json.dumps(payload, separators=(",", ":")).encode(
                            "utf-8"
                        )

        if getattr(adapter, "transforms_openai_chat", False) and rel_path == "/chat/completions":
            return await _handle_claude_chat(request, cred, body)

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)

        async def _send_upstream(active_cred: UpstreamCredential):
            upstream_url = f"{active_cred.base_url.rstrip('/')}{rel_path}"
            # Preserve the raw percent-encoded query. ``request.query_string``
            # is decoded by aiohttp and corrupts values when interpolated into
            # a new URL (for example %252f -> /). Never log query contents.
            raw_path = str(request.raw_path or "")
            if "?" in raw_path:
                raw_query = raw_path.split("?", 1)[1]
                if raw_query:
                    upstream_url = f"{upstream_url}?{raw_query}"

            fwd_headers = _filter_request_headers(request.headers)
            fwd_headers = _strip_owned_headers(
                fwd_headers,
                adapter.get_owned_upstream_header_names(),
            )
            fwd_headers = _merge_adapter_headers(
                fwd_headers,
                adapter.get_upstream_headers(active_cred),
            )
            fwd_headers["Authorization"] = (
                f"{active_cred.token_type} {active_cred.bearer}"
            )

            logger.debug(
                "proxy: forwarding %s %s -> %s%s (body=%d bytes)",
                request.method,
                rel_path,
                active_cred.base_url.rstrip("/"),
                rel_path,
                len(body),
            )

            return await _open_upstream_request(
                method=request.method,
                url=URL(upstream_url, encoded=True),
                body=body,
                headers=fwd_headers,
                timeout=timeout,
            )

        async def _open_upstream(active_cred: UpstreamCredential):
            try:
                return await _send_upstream(active_cred)
            except RuntimeError as exc:
                return _json_error(500, str(exc)), None
            except aiohttp.ClientError as exc:
                logger.warning("proxy: upstream connection failed: %s", exc)
                return (
                    _json_error(
                        502,
                        f"upstream connection failed: {exc}",
                        code="upstream_unreachable",
                    ),
                    None,
                )
            except asyncio.TimeoutError:
                return (
                    _json_error(
                        504,
                        "upstream request timed out",
                        code="upstream_timeout",
                    ),
                    None,
                )

        session_or_response, upstream_resp = await _open_upstream(cred)
        if upstream_resp is None:
            return session_or_response
        session = session_or_response

        if upstream_resp.status in {401, 429}:
            try:
                retry_cred = await asyncio.to_thread(
                    adapter.get_retry_credential,
                    failed_credential=cred,
                    status_code=upstream_resp.status,
                )
            except Exception as exc:
                logger.warning("proxy: retry credential resolution failed: %s", exc)
                retry_cred = None

            if retry_cred is not None:
                upstream_resp.release()
                await session.close()
                session_or_response, upstream_resp = await _open_upstream(retry_cred)
                if upstream_resp is None:
                    return session_or_response
                session = session_or_response

        if materialize_responses_stream:
            return await _materialize_responses_stream(upstream_resp, session)
        return await _stream_upstream_response(request, upstream_resp, session)

    # /health doesn't go through the upstream
    app.router.add_get("/health", handle_health)
    # Catch-all under /v1 — forwards if the path is allowed.
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)

    return app


async def run_server(
    adapter: UpstreamAdapter,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    shutdown_event: Optional[asyncio.Event] = None,
    *,
    client_auth_token: Optional[str] = None,
) -> None:
    """Run the proxy in the current event loop until shutdown_event is set.

    If shutdown_event is None, runs until cancelled (Ctrl+C or SIGTERM).
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    if adapter.loopback_only and not is_loopback_host(host):
        raise RuntimeError(
            f"{adapter.display_name} proxy is loopback-only; refusing bind host {host!r}."
        )

    app = create_app(adapter, client_auth_token=client_auth_token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(
        "proxy: listening on http://%s:%d/v1 -> %s",
        host,
        port,
        adapter.display_name,
    )

    stop_event = shutdown_event or asyncio.Event()

    # Wire signal handlers when we own the loop's lifetime.
    if shutdown_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)  # windows-footgun: ok
            except NotImplementedError:
                # Windows / restricted environments — Ctrl+C will still
                # raise KeyboardInterrupt and unwind us.
                pass

    try:
        await stop_event.wait()
    finally:
        logger.info("proxy: shutting down")
        await runner.cleanup()


__all__ = [
    "create_app",
    "run_server",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AIOHTTP_AVAILABLE",
]
