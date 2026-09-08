#!/usr/bin/env python3
"""AC5 live verification: real Hindsight traffic through the failover gateway.

Starts the failover gateway from THIS worktree on a spare loopback port with the
real Claude Code + Codex subscription adapters, then drives real
retain/consolidation-shaped requests through it three ways:

  1. both backends healthy      -> served by the preferred backend
  2. Claude forced unavailable  -> fails over to Codex
  3. Codex forced unavailable   -> fails over to Claude

"Forced unavailable" is done WITHOUT touching any credential or live service:
the adapter's base_url is pointed at a closed loopback port, which is exactly
the classified connection-failure case the policy fails over on.

Touches nothing about the running hermes-hindsight-proxy.service or its tunnel.
Prints only status codes, backend names, and payload shapes — never tokens,
prompts, or account identifiers.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from hermes_cli.proxy.adapters import get_adapter  # noqa: E402
from hermes_cli.proxy.gateway import create_failover_app  # noqa: E402
from hermes_cli.proxy.routing import BackendCircuit  # noqa: E402

# A Hindsight retain call is a structured-output extraction: a JSON schema plus
# a short passage. This is that shape, kept tiny so the probe costs almost
# nothing against a real subscription.
RETAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "fact_type": {"type": "string"},
                },
                "required": ["fact", "fact_type"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    # Both upstreams require this on every object node for strict structured
    # output; Anthropic rejects the request outright without it.
    "additionalProperties": False,
}

RETAIN_PASSAGE = (
    "The subscription proxy gained a failover gateway on 2026-09-08. "
    "It routes between the Claude Code and Codex subscriptions."
)


def retain_chat_payload(model: str) -> dict:
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Extract atomic facts from the passage as JSON.",
            },
            {"role": "user", "content": RETAIN_PASSAGE},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "retain", "schema": RETAIN_SCHEMA},
        },
        "max_tokens": 512,
    }


def consolidate_responses_payload(model: str) -> dict:
    """Consolidation is Hindsight's other call: a Responses-family request."""
    return {
        "model": model,
        "instructions": "Merge duplicate facts. Reply with JSON only.",
        "input": (
            '[{"fact":"the proxy gained failover","fact_type":"world"},'
            '{"fact":"the proxy gained a failover gateway","fact_type":"world"}]'
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "consolidate",
                "schema": RETAIN_SCHEMA,
            }
        },
        "max_output_tokens": 512,
    }


class _DeadUpstream:
    """Wrap an adapter so its credential points at a closed loopback port."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def get_credential(self):
        credential = self._inner.get_credential()
        # Port 1 on loopback refuses connections: a real, classified transport
        # failure, with the real credential never leaving this process.
        return type(credential)(
            bearer=credential.bearer,
            base_url="http://127.0.0.1:1/v1",
            token_type=credential.token_type,
            expires_at=credential.expires_at,
        )


async def serve(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(site._server.sockets)[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def call(base_url: str, path: str, payload: dict, token: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base_url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            body = await response.read()
            return {
                "status": response.status,
                "backend": response.headers.get("X-Hermes-Route-Backend", ""),
                "attempts": response.headers.get("X-Hermes-Route-Attempt", ""),
                "request_id_present": bool(
                    response.headers.get("X-Hermes-Request-Id")
                ),
                "body": body,
            }


def describe(label: str, result: dict, *, client_api: str) -> bool:
    """Print a redacted verdict; return whether the call really succeeded."""
    ok = result["status"] == 200
    shape = "-"
    parsed_json_payload = False
    if ok:
        try:
            payload = json.loads(result["body"])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if client_api == "chat":
                shape = payload.get("object", "?")
                content = (
                    payload.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content")
                )
            else:
                shape = payload.get("object", "?")
                content = payload.get("output_text")
            ok = shape in {"chat.completion", "response"}
            # Structured output must actually parse as JSON matching the schema
            # shape Hindsight asked for; a prose answer is a failed retain.
            if isinstance(content, str) and content.strip():
                try:
                    extracted = json.loads(content)
                    parsed_json_payload = isinstance(extracted, dict) and isinstance(
                        extracted.get("facts"), list
                    )
                except json.JSONDecodeError:
                    parsed_json_payload = False
        else:
            ok = False
    else:
        try:
            error = json.loads(result["body"])["error"]
            shape = error.get("code") or error.get("type") or "?"
            # Upstream validation text is provider-side and carries no secret;
            # printing it is how this probe is debuggable at all.
            print(f"      upstream said: {str(error.get('message'))[:300]}")
        except Exception:
            shape = "unparseable"
            print(f"      raw body: {result['body'][:300]!r}")

    print(
        f"  {label:38s} status={result['status']:<4} "
        f"backend={result['backend'] or '-':<14} "
        f"attempts={result['attempts'] or '-':<3} "
        f"reqid={'y' if result['request_id_present'] else 'n'}  "
        f"shape={shape:<16} schema_ok={'y' if parsed_json_payload else 'n'}"
    )
    return ok and parsed_json_payload


async def scenario(name: str, adapters, token: str, expect_backend: str) -> bool:
    circuit = BackendCircuit(failure_threshold=99, cooldown_seconds=0)
    app = create_failover_app(adapters, client_auth_token=token, circuit=circuit)
    runner, base = await serve(app)
    passed = True
    try:
        print(f"\n{name}")
        retain = await call(
            base, "/v1/chat/completions", retain_chat_payload(MODEL_BY[expect_backend]), token
        )
        passed &= describe("retain  (chat/completions)", retain, client_api="chat")
        passed &= retain["backend"] == expect_backend

        consolidate = await call(
            base, "/v1/responses", consolidate_responses_payload(MODEL_BY[expect_backend]), token
        )
        passed &= describe("consolidate (responses)", consolidate, client_api="responses")
        passed &= consolidate["backend"] == expect_backend

        if retain["backend"] != expect_backend or consolidate["backend"] != expect_backend:
            print(f"  !! expected backend {expect_backend}")
    finally:
        await runner.cleanup()
    return bool(passed)


MODEL_BY = {
    "claude-code": "claude-sonnet-4-6",
    # VERIFIED against the live upstream 2026-09-08: gpt-5 / gpt-5-codex /
    # codex-mini-latest are all refused for a ChatGPT-account Codex session;
    # gpt-5.5 is accepted.
    "openai-codex": "gpt-5.5",
}


async def main() -> int:
    token = secrets.token_urlsafe(32)
    results = []

    claude = get_adapter("claude-code")
    codex = get_adapter("openai-codex")

    results.append(await scenario(
        "1. Both backends healthy (preferred = claude-code)",
        [get_adapter("claude-code"), get_adapter("openai-codex")],
        token,
        "claude-code",
    ))

    results.append(await scenario(
        "2. Claude forced unavailable -> Codex serves",
        [_DeadUpstream(get_adapter("claude-code")), get_adapter("openai-codex")],
        token,
        "openai-codex",
    ))

    results.append(await scenario(
        "3. Codex forced unavailable -> Claude serves",
        [_DeadUpstream(get_adapter("openai-codex")), get_adapter("claude-code")],
        token,
        "claude-code",
    ))

    print()
    if all(results):
        print("AC5 LIVE VERIFICATION: PASS — real retain + consolidation succeeded")
        print("through the gateway with each provider forced unavailable in turn.")
        return 0
    print(f"AC5 LIVE VERIFICATION: FAIL — {results.count(False)}/3 scenarios failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
