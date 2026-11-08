# Human-input bridge (removable T1 fork module)

This is an **opt-in runtime integration**, disabled by default. Nothing starts on import.
Gateway TurnRunner and the TUI plugin-UI installer bind one source owner per turn,
pump IPC intents, then close and invalidate that owner at teardown. Telegram network
I/O and the authenticated web host remain in the separate fork-ask plugin lane.

## Integration

In the original source owner process, with a verified recipient and explicit
profile-local **private directory** (0700):

```python
from hermes_fork.human_input_bridge import Mailbox, OwnerBridge, bind_ask_bridge

mailbox = Mailbox(explicit_path)  # SQLite file is 0600; stdlib only
owner = OwnerBridge(mailbox, recipient)
bind_ask_bridge(actual_turn_agent, owner)
# Run the existing Ask tool normally, then at teardown:
bind_ask_bridge(actual_turn_agent, None)
owner.close()
```

Only `fork-ask/questions` at `PluginUI.request` is projected. An opaque per-call
`fork_input_nonce` travels in fork-owned `plugin.request` params (not the Ask
payload or browser envelope). This binds the observed source registration exactly,
even for identical concurrent questions. The existing source call still owns the
wait, deadline, interruption, cancellation and result; no replacement request
registry or approval waiter was introduced. Requests answered too quickly to be
observed need no escalation. Unbound UI behavior is unchanged.

The observer pumps intents while that original call waits. It uses the real
`tui_gateway.server_requests.resolve_response`, or
`gateway.session_server_requests.resolve` with exact stream **and** session
identity. Owner bindings retain a random per-instance epoch, immutable source
snapshot and digest; a changed request id/payload/recipient/expiry cannot be
retargeted. No owner id or raw source request id belongs in browser/callback data.

For explicit source integrations, `OwnerBridge.watch_tui`, `watch_authority` and
`watch_approval` bind an already-pending **exact request id**. Supply the original
absolute expiry and safely projected payload, then call `owner.drain()` in that
same process. Do not publish a new request in the Telegram/backend process and
pretend its owner is the gateway. Do not pass a session id as an owner capability.

`watch_approval` calls the original
`resolve_gateway_approval(session_key, choice, request_id=...)` and attaches
`observe_gateway_settle` additively; it never replaces an existing settle hook.
A consumed approval intent remains `submitting` until the original waiter emits
its authoritative receipt. A delayed receipt preserves acceptance before expiry;
it does not extend the original expiry or permit a new late intent.

## Stable mailbox contract

`Mailbox(path)` implements every method in the build contract: `publish`, `get`,
`ready`, `mark_delivered`, `propose`, `intents`, `acknowledge`, `update_draft`.
Connections are per-operation; `BEGIN IMMEDIATE` serializes independent process
writers, including draft revisions and simultaneous proposals. No in-memory queue
is used for IPC. The owner-only correlation state intentionally stays in the source
process; a fresh owner instance cannot adopt prior-generation intents.

- `ready(now=None)` is a non-mutating view of due, pending, unexpired, unsent,
  unattempted rows. Default due time is publication +120 seconds; draft changes
  never extend expiry. Messaging Ask is due immediately, other surfaces use the delay.
- `claim_delivery(handle)` atomically persists the single network attempt before
  sending. `mark_delivered` still accepts after a claim, checks bound chat/topic,
  records one message and never settles. A crash after claim may lose delivery;
  it must never cause a retry. `delivered(bot_id=None, limit=1000)` returns private
  recovery rows newest-first (limit bounded to 10000); presenters retire stale buttons.
- `propose` requires an already authenticated responder matching all four recipient
  fields. Untrusted JSON is **not** authentication. Telegram signature/bot identity
  and freshness verification belongs in the transport backend before this call.
- `propose` queues one intent, increments revision and changes only to `submitting`.
  Duplicate/foreign/stale/expired proposals return false without consuming anything.
- Approval responses must be exactly `{choice: <explicitly allowed choice>}`.
  Ask API intent wrappers are `{action: 'submit'|'cancel', payload: ...}`. Owner
  adapters unwrap submissions and map cancellation to an actual cancelled outcome,
  not an empty submitted answer. Original Desktop envelopes are also supported.
- Only the source owner acknowledges terminal status. `acknowledge` is immutable
  terminal CAS; wrong owners and late submissions are rejected. Source adapters
  maintain trusted receipt timestamps privately for acknowledgments delayed beyond
  an already-accepted request's deadline.
- `update_draft` returns the updated private row; stale/settled raises `Conflict`
  (ValueError, status_code 409), expired raises `Expired` (410), unknown raises
  KeyError. Revision is an exact nonnegative integer, never bool/float.
- `get` and `intents` are **private backend rows** including owner/source ids.
  Transport code must whitelist the fixed public envelope fields, never serialize
  the whole row to browsers. Caller projects secrets out before publish; the
  mailbox additionally whitelists only ask/clarify/approval request kinds.

The trusted boundary is one local OS account with a private directory/database.
This is not a sandbox against another process already running as that same account.
SQLite data is unencrypted and retained; deployment needs a profile retention
policy. No automatic expired-row reaper or crash/orphan adoption is claimed.

## Opt-in config and host API

```yaml
human_input_bridge:
  enabled: false
  delay_seconds: 120
  telegram:
    user_id: 123
    chat_id: 123
    thread_id: null
    bot_id: 456
  # mailbox_path: human-input-bridge/mailbox.sqlite3
  mini_app_url: https://example.invalid/telegram-ask/
  http: {host: 127.0.0.1, port: 8789}
```

Core reads `load_config_readonly()` once per turn inside the routed profile scope;
`hermes_fork.config.human_input_bridge(config, home=get_hermes_home())` validates it.
The default path is `<profile>/human-input-bridge/mailbox.sqlite3`; relative paths
are profile-relative and paths escaping the profile are rejected. No bot token is
accepted here. The plugin host reads the same config once, registers a dormant
`ctx.register_telegram_handler(factory)` factory, and gets the token only from the
connected native application after exact configured bot-id validation. No second
poller. The plugin owns `mini_app_url`, HTTP startup/teardown, TLS/auth and native UI.

`runtime.bind_agent(agent, config=None, messaging=False)` returns an owner or None;
`release_agent(agent)` invalidates the owner and joins its pump. Runtime installers:

- One four-line T2 `human-input-runtime` anchor at the end of
  `gateway/run_turn_runner.py` installs wrappers for real turn bind/teardown,
  `_approval_notify_sync`, `_send_shared_clarify`, and canonical clarify publication.
- Existing `hermes_fork.ui.gateway.install(server)` installs sidecar turn ownership
  at its real usage-ticker lifecycle and exact-id source registration hooks.
- Messaging Ask attaches a mailbox-backed transport to a source-process
  `SessionEvents` and uses the **real** `ask_viewer_result` waiter. Registration
  publishes the projection immediately; only the original source resolver returns
  the tool result. This is not a fake pending record in the bot process.
- Native approval projections force-redact command/description before persistence,
  expose only once/deny and always resolve the exact queue id, never FIFO.
- Ordinary clarify batches validate the complete exact question-id set and commit
  under the original source registry lock, preserving already locked answers.
  These private registry seams are pinned by production integration tests.

## Side-question owner RPC

`propose_side(handle, question_id, question, responder)` returns an opaque id or
False. It requires pending/unexpired state, exact authenticated recipient scope,
a question belonging to the request (id or qid), text up to 2000 chars, no other
inflight side request, and at most eight side requests per main request. No main
revision/draft change. Approval side requests are rejected.

`side_requests(owner_id)` is private source-owner work: entries contain handle and
side_id. `acknowledge_side(handle, side_id, owner_id=..., answer=...)` accepts only
the exact pending owner and bounded (16000 chars) answer. `get_sides(handle)` is
browser safe: **only** `{id, question_id, question, status, answer}` per item.

The source pump executes existing `agent.side_question.answer_side_question` on a
snapshot of `_session_messages` with `parent_agent` and live `main_runtime` settings,
preserving prompt-cache parity and read-only tools. It never starts an ordinary
user turn, edits parent history, or resolves the main waiter. Answers are force
redacted before persistence. Closing/settling interrupts active side forks; late
answers cannot be acknowledged. Plugin submit validation must build side metadata
from acknowledged mailbox answers rather than accepting browser-authored answers.

## Remaining operational boundary

No live Telegram/model/HTTP call is made by the core tests. The plugin lane owns
network delivery and stale-owner retirement after source-process crashes (a fresh
owner never adopts the prior owner epoch). SQLite rows are retained and unencrypted;
retention/reaping remains deployment work. No live deployment, unit restart, push
or merge is included. Native approval hook routing is installed; its real SDK send
is not exercised here. Config changes take effect on a new turn, not every pump tick.

## Verification

Run the owned `tests/hermes_fork/test_human_input_bridge*.py` plus
`tests/hermes_fork/ui` through `scripts/run_tests.sh`, with an isolated HOME and an
actual dev/test virtualenv set via `HERMES_PYTHON`. The IPC tests use multiprocessing
**spawn**: separate source-owner and responder processes exchange only SQLite data;
source resolution is the real server-request primitive. A second process race tests
one-winner proposal CAS. Approval integration only submits **deny** to a harmless
isolated waiter; no command is executed. Clock tests control delay/expiry without
wall-clock sleeps. Existing UI regression tests cover unbound behavior.
