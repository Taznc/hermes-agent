# Session archive backend verification

Platform: Linux x86_64, Python 3.14.7. Local work only; no restart, merge, push, live database writes or upstream issue/PR.

## RED / GREEN

- Storage TDD: `test_active_turn_rejects_storage_archive` failed with `DID NOT RAISE ValueError` (1 failed, 1 passed), then both tests passed after the storage hook.
- Queued API TDD: `test_queued_structured_api_run_blocks_its_session` failed with `DID NOT RAISE ValueError` (1 failed, 1 passed), then both passed after run-status admission locking.
- Durable ownership TDD: both `test_durable_async_dispatch_blocks_without_live_registry` and `test_checkpoint_process_blocks_without_live_registry` failed with `DID NOT RAISE ValueError`; both then passed with the durable fallback.
- Transport refusal tests were run before transport changes and failed, then passed with HTTP 409 / RPC 4024 mapping and an atomic transaction facade.
- Concurrency: a second thread acquires a real SessionDB turn lease AFTER advisory discovery and BEFORE BEGIN; rejected compound mutation preserves title, archived, hidden, pinned and last_read_at. A separate stale-client discovery test similarly acquires work on another thread before archive.

## Final diff-derived suite

Run from this checkout, using an isolated scratch HOME:

```sh
mkdir -p "$TMPDIR/archive-backend-home"
HOME="$TMPDIR/archive-backend-home" \
HERMES_PYTHON=/home/hermes/.hermes-next/installs/5116e94174e0c2f0/test-environment/gen-1d8d78a28c324721a014eb024cc24162/venv/bin/python \
bash scripts/run_tests.sh -j 4 --file-timeout 120 --file-retries 0 \
  tests/hermes_fork/test_session_archive_protection.py \
  tests/hermes_fork/test_session_archive_liveness.py \
  tests/hermes_fork/test_session_archive_wire.py \
  tests/hermes_fork/test_session_archive_atomicity.py \
  tests/hermes_fork/test_session_archive_api_queue.py \
  tests/hermes_fork/test_session_archive_durable.py \
  tests/hermes_fork/test_session_archive_requests.py \
  tests/hermes_fork/gateway \
  tests/tui_gateway/test_session_archive_rpc.py \
  tests/tui_gateway/contracts/test_generated.py \
  tests/hermes_state/test_session_archived_round_trip.py \
  tests/hermes_state/test_pinned_archived_sidebar.py \
  tests/hermes_state/test_delete_session_write_guards.py \
  tests/gateway/test_session_api.py \
  tests/tools/test_process_checkpoint_readopt.py \
  tests/tools/test_process_registry_list_exit.py \
  tests/tools/test_async_delegation_stale_profile_scope.py \
  tests/hermes_cli/test_web_server_auto_archive_gateway_lock.py \
  tests/hermes_cli/test_web_server_auto_archive_profile_config.py \
  tests/tools/test_process_registry.py
```

Result: **20 files, 212 passed, 0 failed, 4 Windows-only skipped**.

Additional gates: `uvx ruff check`, `uvx ruff check --select I`, and `uvx ruff format --check` on `hermes_fork/session_archive`, the new RPC module, and all new archive tests: pass (14 files formatted). `git diff --check`: pass. Fresh isolated-HOME imports of SessionDB, REST router, messaging API, TUI gateway and fork archive module: `archive imports OK`. Generated upstream RPC contracts test passes; fork registration is additive.

An earlier suite invoked with the inherited serving HOME failed only the existing interim-commentary SSE test. Isolated scratch HOME made all 36 session API tests pass, and the final entire diff-derived suite is green. A HEAD-source baseline probe of the API modules also passed all 36 tests under scratch HOME; this is an environment-isolation finding, not a claimed code fix.

## Contract and protected scope

- REST PATCH and messaging PATCH archive=true: HTTP 409, `error.code=session_archive_blocked`, stable `blockers` list.
- session.archive archive=true: RPC error 4024; archive does not close/interrupt work or modify a rejected draft's pending flag.
- fork.session.archive_status: `{session_key, archivable, blockers}`; read-only advisory, never substituted for write-time admission.
- Storage set_session_archived rejects owned work inside BEGIN IMMEDIATE. Existing bulk archive uses this entry point; retention skips blocked rows and reports successful archives only.
- Compression lineage, durable/live session ids, reconnect aliases, descendant ownership, same-profile child/process provenance, server requests (including plugin UI), queued prompts, queued/waiting structured API runs, turn/compression leases, durable async running/finalizing/pending delivery, checkpoint processes and owner-pinned mailbox deliveries are observed through actual registries and built-in liveness authorities.
- Idle OPEN alone is allowed. Unarchive follows the original path. Rejected compound PATCH preserves all title/flags; successful title/flag updates reuse canonical setters in one transaction.

## Remaining boundaries

- No full-suite or Windows execution was attempted. The four platform skips are explicit.
- Existing opaque sandbox checkpoints conservatively block while their producer cannot prove completion.
- Cross-process turn/compression and durable async admission share state.db transaction serialization. Cross-process checkpoint/mailbox publishing uses independent stores: discovery sees existing owned work, but there is no global producer mutex fencing a brand-new publish between discovery and commit.
- Structured API run admission is atomically observed in-process. A non-idempotent queued API request in a DIFFERENT gateway process, before it obtains the shared turn lease, has no authoritative profile-owned status projection readable here; that pre-lease window remains uncovered.
- Bulk archive retains its existing per-target semantics; prior idle targets may already have committed before a later busy target raises.

## Parent-owned budget reconciliation

Do not edit fork-budget-next.json in this task. Register the new fork-owned `hermes_fork/session_archive/` tree, `hermes_fork/gateway/methods_session_archive.py`, seven new `tests/hermes_fork/test_session_archive_*.py` files, and the existing fork gateway registry/test changes. Eight upstream anchor sites have checkout-local deltas <=5 lines:

- session-archive-messaging-guard: gateway/platforms/api_server.py (4)
- session-archive-api-work: gateway/platforms/api_server_runs.py (3)
- session-archive-rest-guard: hermes_cli/web_routers/sessions.py (4)
- session-archive-retention-guard: hermes_state_maintenance.py (3)
- session-archive-storage-guard: hermes_state_sessions.py (4)
- session-archive-subagent-owner: tools/delegate_tool_registry.py (3)
- session-archive-process-owner: tools/process_registry.py (5)
- session-archive-rpc-guard: tui_gateway/methods_session.py (4)

Every anchor imports a reachable fork module. The process provenance field is checkpointed; child provenance is frozen at registration. No target was indexed.
