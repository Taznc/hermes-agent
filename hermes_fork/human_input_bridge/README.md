# Human-input bridge (removable T1 fork module)

This is IPC infrastructure plus **opt-in** source adapters, not an enabled Telegram
feature. Nothing starts on import. No mailbox path, recipient, credentials, config
key, service, network listener or bot connection is inferred.

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

- `ready(now=None)` is a non-mutating view of due, pending, unexpired, unsent rows.
  Default due time is publication +120 seconds; draft changes never extend expiry.
- `mark_delivered` checks bound chat/topic, records one message and never settles.
  There is no exactly-once network-send guarantee; callers must run one presenter.
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

## Deliberately unwired anchors / remaining work

1. Runtime must construct the owner with an explicit profile-local path and verified
   recipient, bind the actual turn agent, and close it on teardown. There is no
   automatic enablement/config discovery in this change.
2. Native messaging turns without an existing `PluginUI` request still need an
   owner-side Ask rich-transport lifecycle anchor. The separate bot/backend process
   cannot fabricate an owner or settle gateway memory. Existing UI source primitives
   decline immediately with no attached viewer; this module does not override that.
3. Native approval lifecycle needs a minimal parent-reviewed anchor where the
   original request id is available: `gateway/run_turn_runner.py::_approval_notify_sync`
   (or the appropriate sidecar approval notifier in `tui_gateway/server.py`). Call
   `watch_approval` there and arrange owner-side pumping/lifecycle. **Not installed**.
4. Native clarify publication needs a corresponding parent-reviewed hook at
   `gateway/run_turn_runner.py::_clarify_callback_sync` and/or the source request
   publication/wait lifecycle. Exact-id watch adapters exist, but no automatic
   clarify hooking, batch-progress translation, or partial-answer merge is claimed.
5. Side-question owner execution, bot registration, authenticated Mini App server,
   actual native renderer and deployment belong to the other lanes/parent. This
   module does not invent side-question answers or perform any live Telegram send.

## Verification

Run the owned `tests/hermes_fork/test_human_input_bridge*.py` plus
`tests/hermes_fork/ui` through `scripts/run_tests.sh`, with an isolated HOME and an
actual dev/test virtualenv set via `HERMES_PYTHON`. The IPC tests use multiprocessing
**spawn**: separate source-owner and responder processes exchange only SQLite data;
source resolution is the real server-request primitive. A second process race tests
one-winner proposal CAS. Approval integration only submits **deny** to a harmless
isolated waiter; no command is executed. Clock tests control delay/expiry without
wall-clock sleeps. Existing UI regression tests cover unbound behavior.
