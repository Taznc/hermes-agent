---
sidebar_position: 15
title: "Subscription Proxy"
description: "Use your Nous Portal subscription (or other OAuth provider) as an OpenAI-compatible endpoint for external apps"
---

# Subscription Proxy

The subscription proxy is a local HTTP server that lets external apps —
OpenViking, Karakeep, Open WebUI, anything that speaks OpenAI-compatible
chat completions — use your Hermes-managed provider subscription as their
LLM endpoint. The proxy attaches the right credentials (refreshing them
automatically) so the app never needs a static API key.

This is different from the [API server](./api-server.md):

| | API server | Subscription proxy |
|---|---|---|
| What it serves | Your agent (full toolset, memory, skills) | Raw model inference |
| Use case | "Use Hermes as a chat backend" | "Use my Portal sub from another app" |
| Auth | Your `API_SERVER_KEY` | Provider-specific; Claude Code and Codex require an owner-only client bearer |
| Tool calls | Yes — the agent runs tools | No — passthrough only |

Use the API server when you want the **agent** as a backend. Use the
proxy when you just want **the model** through your subscription.

## Quick Start

### 1. Log into your provider (one-time)

```bash
hermes portal
```

This opens your browser for the Nous Portal OAuth flow. Hermes stores
the refresh token in `~/.hermes/auth.json` — the same place all Hermes
provider logins live.

### 2. Start the proxy

```bash
hermes proxy start
```

```
Starting Hermes proxy for Nous Portal
  Listening on:  http://127.0.0.1:8645/v1
  Forwarding to: (resolved per-request from your subscription)
  Use any bearer token in the client — the proxy attaches your real credential.
```

Leave this running in the foreground. Use `tmux`, `nohup`, or a systemd
unit if you want it to survive logout.

### 3. Point your app at it

Any OpenAI-compatible app config takes the same triple:

```
Base URL:   http://127.0.0.1:8645/v1
API key:    anything (e.g. "sk-unused")
Model:      Hermes-4-70B    # or Hermes-4.3-36B, Hermes-4-405B
```

The proxy ignores the `Authorization` header from your app and attaches
your real Portal credential to the upstream request. Refreshes happen
automatically when the bearer approaches expiry.

## Available providers

```bash
hermes proxy providers
```

Currently shipped:

- `claude-code` — locally logged-in Claude Code subscription; Chat Completions are translated to Anthropic Messages
- `openai-codex` (`codex` alias) — OpenAI Codex / ChatGPT OAuth, Responses API only
- `nous` — Nous Portal
- `xai` — xAI / Grok OAuth

More OAuth providers can be added by implementing the `UpstreamAdapter`
interface in `hermes_cli/proxy/adapters/`.

### OpenAI Codex / ChatGPT OAuth

Create a client bearer in an owner-only regular file, then start the proxy from
the Hermes profile that owns the Codex OAuth credential:

```bash
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.hermes/codex-proxy.token
hermes proxy start --provider codex \
  --auth-token-file ~/.hermes/codex-proxy.token
```

On POSIX systems the token file must be owned by the current user and have mode
`0600`; symlinks are rejected. Keep its contents out of logs, command arguments,
and source-controlled configuration.

On Windows the file owner must be the current user (or SYSTEM), and its DACL may
grant access only to that user and SYSTEM. Inherited or explicit allow entries
for principals such as `Everyone` or `BUILTIN\\Users` are rejected.

Codex forwards only `/v1/responses` and `/v1/models`. The adapter attaches the
native Codex `originator`, `User-Agent`, and JWT-derived
`ChatGPT-Account-ID` headers after stripping client-supplied values, so a
loopback client cannot spoof account identity. A 401 refreshes the exact failed
pool credential and rotates when refresh fails; a 429 marks the failed credential
exhausted and rotates to another available account.

Point a Responses-compatible client at:

```text
Base URL: http://127.0.0.1:8645/v1
API key:  the bearer stored in ~/.hermes/codex-proxy.token
Model:    a model available to your ChatGPT/Codex account
Transport: OpenAI Responses API
```

Keep this proxy on `127.0.0.1`. Loopback limits network reachability, while the
required client bearer establishes authority to spend the OAuth account owned
by the profile that started it. Missing or incorrect client credentials return
HTTP 401 before Hermes reads credential-pool availability, resolves a bearer,
or contacts the Codex upstream. This applies to `/health` as well as `/v1/*`.

## Check status

```bash
hermes proxy status
```

```
Hermes proxy upstream adapters

  [nous    ] Nous Portal — ready (bearer expires 2026-05-15T06:43:21Z)
```

If you see `not logged in`, run `hermes portal`. If you see
`credentials need attention`, your refresh token was revoked (rare —
happens if you signed out from the Portal web UI) — just re-run
`hermes portal`.

## Allowed paths

The proxy only forwards paths the upstream actually serves. For Nous
Portal:

| Path | Purpose |
|------|---------|
| `/v1/chat/completions` | Chat completions (streaming + non-streaming) |
| `/v1/completions` | Legacy text completions |
| `/v1/embeddings` | Embeddings |
| `/v1/models` | Model list |

Other paths (`/v1/images/generations`, `/v1/audio/speech`, etc.) return
404 with a clear error pointing at the allowed paths. This keeps stray
clients from leaking weird requests to the upstream.

## Configuring OpenViking to use Portal

[OpenViking](https://github.com/volcengine/OpenViking) is a context
database that needs an LLM provider for its VLM (vision/language model
used to extract memories) and embedding model. With the proxy, you can
point its `vlm.api_base` at your local proxy:

Edit `~/.openviking/ov.conf`:

```json
{
  "vlm": {
    "provider": "openai",
    "model": "Hermes-4-70B",
    "api_base": "http://127.0.0.1:8645/v1",
    "api_key": "unused-proxy-attaches-real-creds"
  }
}
```

Then start your proxy in a terminal alongside `openviking-server`:

```bash
# Terminal 1
hermes proxy start

# Terminal 2
openviking-server
```

OpenViking's VLM calls now flow through your Portal subscription. The
embedding model side still needs its own provider — Portal does serve
`/v1/embeddings` but the model selection depends on what your tier
supports; check `portal.nousresearch.com/models`.

## Configuring Karakeep (or any bookmark/summarizer app)

[Karakeep](https://karakeep.app/) takes an OpenAI-compatible API for
bookmark summarization. In its config:

```bash
# Karakeep .env
OPENAI_API_BASE_URL=http://127.0.0.1:8645/v1
OPENAI_API_KEY=any-non-empty-string
INFERENCE_TEXT_MODEL=Hermes-4-70B
```

Same pattern works for Open WebUI, LobeChat, NextChat, or any other
OpenAI-compatible client.

## Configuring Hindsight through a private tunnel

Keep the proxy loopback-bound on the dev VM; do not copy `auth.json` or Claude
Code credentials into the Hindsight container or `docker-host`. Create an
owner-only client bearer, then start it with:

```bash
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.hermes/claude-code-proxy.token
hermes proxy start --provider claude-code --host 127.0.0.1 --port 8645 \
  --auth-token-file ~/.hermes/claude-code-proxy.token
```

From `docker-host`, create an authenticated private tunnel to the dev VM (for
example, `ssh -N -L 8645:127.0.0.1:8645 hermes@dev-vm`). Configure Hindsight's
OpenAI-compatible provider to use the tunnel endpoint and a non-empty dummy
key:

```yaml
provider: openai
base_url: http://127.0.0.1:8645/v1
api_key: <contents of ~/.hermes/claude-code-proxy.token>
model: claude-sonnet-4-6
```

The bearer is required by the proxy and must be delivered to Hindsight through
its normal secret mechanism, not source-controlled configuration. The proxy
resolves the Claude Code subscription locally. Do not bind the proxy to
`0.0.0.0` for this deployment. Verify a non-streaming, streaming, and tool-call
completion through the proxy before applying this configuration to a running
Hindsight deployment.

## Failover across two subscriptions (one ingress, two backends)

A single subscription runs out. When you hold two — a Claude Code plan and a
ChatGPT/Codex plan, with independent quota windows — you can put both behind
**one stable endpoint** and let the gateway switch between them, without the
client ever changing its base URL, its API family, or its config:

```bash
hermes proxy start --provider claude-code,openai-codex \
  --host 127.0.0.1 --port 8645 \
  --auth-token-file ~/.hermes/proxy.token
```

`--provider` becomes an **ordered preference list**. The first backend is
preferred; the rest are fallbacks in the order given. A single name behaves
exactly as before, so nothing about an existing single-provider deployment
changes.

### Topology

```
             ┌──────────────────────────────────────────┐
 client ───▶ │  failover gateway  (the only public face) │
             │   /v1/chat/completions   /v1/responses    │
             └───────────────┬───────────────┬──────────┘
                             │               │
                   direct adapter call   direct adapter call
                             ▼               ▼
                   Claude Code           OpenAI Codex
                (Anthropic Messages)      (Responses)
```

Backends are **private adapters invoked in-process**. A backend never receives
the gateway's own URL and can never call back into it, so a failover cannot
become a loop no matter how the chain is configured.

The transport in front of the gateway (an SSH tunnel, a reverse proxy) stays
**lifecycle-independent from every backend**. Never make a tunnel unit
`Requires=` or `BindsTo=` a provider-specific unit: a capped provider's restart
loop would then tear down in-flight requests that a healthy replacement backend
was serving.

### Protocol normalization

Claude serves Anthropic Messages; Codex serves the OpenAI Responses API. The
gateway normalizes every request to **Chat Completions** internally, speaks each
backend's native wire format, and re-encodes the answer into whichever family
the *client* used. So all four combinations work:

| Client sends | Served by Claude | Served by Codex |
|---|---|---|
| `POST /v1/chat/completions` | ✅ | ✅ |
| `POST /v1/responses` | ✅ | ✅ |

Tools/function calling, JSON-schema structured output, and streaming are
translated in both directions. A client configured for one API family keeps
working when the gateway fails over to a backend that speaks the other — which
is the whole point: with a single-provider proxy, switching ports also meant
switching the client's protocol setting.

The gateway serves exactly `/v1/chat/completions`, `/v1/responses`, and
`/health`. Any other path returns a 404 naming what is available, rather than
being forwarded somewhere it cannot be translated.

### Failover policy — what does and does not switch backends

**Fails over** (the request is retried on the next unvisited backend):

- connection failures and bounded timeouts
- `429` (rate limit / quota exhausted)
- `500`, `502`, `503`, `504`, `529` (provider unavailable or overloaded)
- a backend whose credentials cannot be resolved at all (it is skipped)

**Never fails over** (the response is returned to you as it arrived):

- `400`, `401`, `403`, `404`, `409`, `413`, `422`, and any other 4xx

That asymmetry is deliberate. A validation, auth, or configuration error means
the *request* is wrong; replaying it against your second subscription just
spends a second account's quota to receive the identical refusal.

### Anti-loop guarantees

1. Each request carries gateway-minted route context: a request id, an attempt
   count, and the set of backends already visited.
2. Each distinct backend is attempted **at most once** per client request, and
   total attempts are bounded by the number of configured backends.
3. A fallback selects only an unvisited backend and invokes its adapter
   directly — never the gateway's own URL.
4. Client-supplied `X-Hermes-Request-Id` / `X-Hermes-Route-*` headers are
   ignored entirely: they are never consulted for routing and never forwarded
   upstream. The gateway mints its own and returns them on the response.
5. On exhaustion the **last classified error** is returned. Selection is never
   restarted.

### Visibility

Every response carries:

| Header | Meaning |
|---|---|
| `X-Hermes-Request-Id` | Gateway-minted id for this client request |
| `X-Hermes-Route-Backend` | Which backend actually served it |
| `X-Hermes-Route-Attempt` | How many backends were tried (1 = no failover) |

Each attempt also emits one structured log line at INFO:

```
proxy.route request_id=<id> attempt=2 backend=openai-codex status=200 ok=True
            reason=ok failover_eligible=False
```

Route telemetry carries **only** gateway-minted identifiers and fixed
classification labels — never prompts, tokens, response bodies, or account
identifiers.

`GET /health` reports each backend's authentication state plus live circuit
state (`open`, `failures`, `opens_for_seconds`, `probe_in_flight`).

### Recovery: circuit breaker behaviour

A backend that keeps failing is put in cooldown rather than probed on every
request — re-probing a capped account wastes a round trip per call and can
extend the provider's own rate-limit window.

- After a threshold of consecutive failures the backend's circuit **opens** and
  it stops being attempted; traffic goes straight to the next backend.
- If the upstream sent a `Retry-After`, that validated deadline **overrides**
  the local cooldown, so no probe fires before the provider says the quota is
  back.
- When the cooldown expires the circuit goes **half-open** and admits exactly
  **one** probe request. Concurrent requests are refused the probe slot, so a
  recovering provider never sees a thundering herd.
- Any successful response closes the circuit immediately and resets the count.

To force a recovery check early, restart the proxy — circuit state is
in-process and is not persisted.

### Duplicate writes and prompt caching — the real tradeoffs

**Duplicate writes.** Failover is only possible *before any byte of the
response has been committed to the client*. For a non-streaming request the
gateway holds the whole answer before it writes anything; for a streaming
request no failover is possible once `prepare()` and the first write have
happened. A failed-over attempt therefore provably never produced a response
the client could have persisted, so a retry cannot cause a duplicate write in
a storage client such as Hindsight — the storage tier only writes after it
receives our response. The `X-Hermes-Request-Id` header is returned so a client
that wants defence in depth can dedupe on it.

The residual case is honest to state: if a backend completed the work but the
response was lost in transit (connection reset after the upstream finished),
the gateway will retry on the second backend. That spends the first
subscription's tokens without a response reaching the client. It cannot create
a duplicate *client-side* write, but it is not free.

**Prompt caching.** Every switch between backends invalidates the provider-side
prompt cache. The first request on the new backend re-reads the entire
conversation at full input price, and so does the first request back. A
workload that bounces between subscriptions costs materially more than one that
stays put. That is exactly what the circuit breaker's cooldown is for: staying
on the healthy backend for the duration of an outage is cheaper than
alternating.

## Exposing on LAN

By default the proxy binds `127.0.0.1` (localhost only). To let other
machines on your network use it:

```bash
hermes proxy start --host 0.0.0.0 --port 8645
```

⚠ **Be aware:** anyone on your network can now use your Portal
subscription. The proxy has no auth of its own — it accepts any bearer.
Use a firewall, VPN, or reverse proxy with proper auth if you expose
this beyond your trusted network.

The `claude-code` and `openai-codex` adapters are stricter: they reject every
non-loopback bind, including `0.0.0.0`, and require an owner-only client bearer
even on loopback. Loopback alone is not an identity boundary on a multi-user
host. They must remain on `127.0.0.1`, `::1`, or `localhost`.

## Rate limits

Your Portal tier's RPM/TPM limits apply across the whole proxy. The
proxy doesn't fan out or pool — it's a single bearer with your full
subscription quota. Monitor usage at
[portal.nousresearch.com](https://portal.nousresearch.com).

## Architecture

Single-provider mode is intentionally minimal. Per request:

1. Receive a request on an adapter-allowed `/v1/*` path
2. Validate client authority when the adapter requires it
3. Look up the adapter's current credential (refresh if expiring)
4. Forward the request body verbatim, replacing `Authorization` with the upstream bearer
5. Stream the response back unchanged (SSE preserved)

OpenAI-compatible upstreams are forwarded unchanged. Non-compatible subscription
providers use a narrow, documented wire translation (Claude Code translates
Chat Completions to Anthropic Messages). There is no request-body logging and no
agent loop.

Failover mode (two or more backends) adds an ingress that owns protocol
normalization and backend selection; see
[Failover across two subscriptions](#failover-across-two-subscriptions-one-ingress-two-backends)
above. Code layout:

| File | Owns |
|---|---|
| `hermes_cli/proxy/server.py` | Single-provider pass-through; entry point for both modes |
| `hermes_cli/proxy/gateway.py` | Failover ingress: selection, client-family encoding, telemetry |
| `hermes_cli/proxy/routing.py` | Failure classification, route context, circuit breaker |
| `hermes_cli/proxy/legs.py` | One backend's wire protocol, spoken over real HTTP |
| `hermes_cli/proxy/claude_translate.py` | Chat Completions ⇄ Anthropic Messages |
| `hermes_cli/proxy/responses_translate.py` | Chat Completions ⇄ OpenAI Responses |

## Future: more OAuth providers

The adapter system is pluggable. Adding a new provider (e.g.
HuggingFace, GitHub Copilot's chat endpoint) requires implementing
`UpstreamAdapter` in `hermes_cli/proxy/adapters/<provider>.py` and registering
it in `adapters/__init__.py`.

A provider that is not OpenAI-compatible at the protocol level declares its
`wire_protocol` (`"openai-chat"`, `"openai-responses"`, or
`"anthropic-messages"`) and the failover gateway routes it through the matching
leg. A genuinely new wire format needs one new `BackendLeg` subclass in
`legs.py` translating to and from canonical Chat Completions — not an entry in
an N×M translation matrix.
