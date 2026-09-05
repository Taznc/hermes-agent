# Infra-death classification, durable timeout intent, bounded interruptions,
# and provider backoff for the kanban dispatcher

## Goal

The dispatcher's failure budget (`kanban.failure_limit` / `--max-retries`)
exists to stop the dispatcher thrashing forever on a task that keeps
failing for reasons the worker itself caused. It was never meant to
punish a task whose worker was killed by something *outside* the task —
a gateway restart, a VM reboot, or a provider quota wall.

This adds an `infra` classification to the dispatcher's exit-reap path
(`detect_crashed_workers` / `_classify_dead_worker` in
`hermes_cli/kanban_db_dispatch.py`) so these deaths stop consuming the
failure budget, while everything that IS a genuine task failure —
nonzero exits, excluded signals, the dispatcher's own `--max-runtime`
kill, iteration-budget exhaustion — keeps counting exactly as it does
today. Because a naive "never count infra deaths" policy could let a
task loop forever without ever touching the failure budget, three
correctness properties are load-bearing and independently tested:

1. **The signal allowlist is explicit and narrow** — only `SIGTERM` and
   `SIGKILL` are infra-eligible. Every other signal (`SIGABRT`, `SIGSEGV`,
   `SIGPIPE`, ...) is always a legit, counted failure, because those
   indicate a crashed process, not a cleanly (if externally) terminated
   one.
2. **The dispatcher's own timeout kill is durable across a restart.**
   Before `enforce_max_runtime` sends SIGTERM/SIGKILL for a
   `max_runtime_seconds` breach, it persists a SQLite-backed intent row.
   If the dispatcher process dies between sending that signal and
   reaping the worker, a *different* dispatcher process reaping the
   worker later still resolves the death as the dispatcher's own kill
   (legit, counted) — never as an "external" infra signal.
3. **Repeated interruption cannot evade the failure budget forever.** A
   persistent per-task streak counts consecutive infra classifications;
   once it exceeds `kanban.max_infra_interruptions` the death is instead
   routed through the ordinary counted-failure path (feeding the same
   circuit breaker as any other crash), with an operator-visible reason
   recorded on the event. The streak survives redispatch and is reset
   ONLY by a genuine non-interruption terminal outcome (e.g.
   `kanban_complete`) or an explicit operator `kanban_unblock` — never
   merely by being re-claimed and re-run.

## Categories

| Category | Meaning | Failure budget | Event kind |
|---|---|---|---|
| `legit` | The task itself is at fault, or the dispatcher took a deliberate, accounted action (its own `--max-runtime` kill), or a signal off the allowlist. | Counts (`consecutive_failures` += 1; may trip `gave_up`). | `crashed` / `timed_out` (unchanged) |
| `infra` | The worker died for a reason external to the task: an *allowlisted* SIGTERM/SIGKILL the dispatcher did not send itself, a dead PID discovered inside the dispatcher's own startup window (gateway restart / VM boot), or a provider 429/quota exit with any retry-after signature (including malformed/missing). | Does NOT count directly. Bumps the persistent interruption streak; task re-queues to `ready` (or `scheduled` when a provider pause was registered) immediately. | `interrupted` |
| streak-exceeded `infra` | An `infra` death that pushed the per-task streak past `kanban.max_infra_interruptions`. | Counts, exactly like a `legit` crash (`force_trip` against the same breaker). | `crashed` / `gave_up` |
| `unknown` (legacy) | Reap registry has no record and none of the infra signals matched — indistinguishable today from a genuine crash. | Counts (unchanged; this is the existing `unknown` → `crashed` path). | `crashed` |

`kanban.count_infra_failures: true` collapses `infra` back into `legit`
for every rule above (restores pre-classification behaviour) — the
signal allowlist, timeout-kill durability, and provider backoff cap are
still enforced independently of this flag.

## Decision table

Evaluated by the pure function `classify_infra_exit()` in
`hermes_cli/kanban_db.py`. Inputs are facts the caller
(`_classify_dead_worker`) gathers from existing and new state; the
function itself does no I/O.

| exit_kind (from `_classify_worker_exit`) | Extra signal | Category | reason |
|---|---|---|---|
| any | quota/429 signature found in the worker's final log lines (`quota_signal_dict`) | `infra` | `quota` |
| `signaled` | `signal_number` NOT in `{SIGTERM, SIGKILL}` | `legit` | `signal_<N>` (ALWAYS legit, regardless of `dispatcher_killed`) |
| `signaled` | `signal_number` in `{SIGTERM, SIGKILL}` AND `dispatcher_killed` True (a pending or just-consumed durable timeout-kill intent exists for this task/pid) | `legit` | `dispatcher_kill` |
| `signaled` | `signal_number` in `{SIGTERM, SIGKILL}` AND `dispatcher_killed` False | `infra` | `external_signal` |
| `unknown` (no reap record — the existing `pid N not alive` path) | this process was marked as a real dispatcher loop (`mark_dispatcher_process_started()`) less than `kanban.infra_startup_window_seconds` (default 120s) ago | `infra` | `startup_window` |
| `unknown` | not marked as a dispatcher loop, or marked more than the window ago | `legit` | `unknown` |
| `nonzero_exit` | none of the above | `legit` | `nonzero_exit` |
| `clean_exit` / `rate_limited` | n/a — untouched, existing dedicated handling | (unchanged) | n/a |

Regression guards (explicit, tested in
`tests/hermes_cli/test_kanban_infra_failure_classification.py`):

* `nonzero_exit` NEVER becomes `infra` on its own (only an explicit quota
  log signature can override it — that is a real quota death that
  happened to exit nonzero, not a generic bug).
* SIGABRT / SIGSEGV / SIGPIPE (and every signal off the allowlist) are
  ALWAYS legit and reach `gave_up` after enough repeats, even with
  `dispatcher_killed=True` or inside the startup window — the allowlist
  check runs first and is exhaustive.
* Iteration-budget exhaustion (`Iteration budget exhausted (N/N)`) is
  recorded by the still-running worker calling `_record_task_failure`
  directly, BEFORE the process exits — the task is no longer
  `status='running'` by the time `detect_crashed_workers` would ever see
  it, so this path is structurally untouched by this change.
* The dispatcher's own `--max-runtime` kill (`enforce_max_runtime`)
  keeps its own synchronous accounting (`timed_out` + `_record_task_failure`)
  exactly as today, AND persists a durable SQLite timeout-kill intent
  immediately before sending each signal
  (`kanban_timeout_kill_intents`, keyed on `task_id`/`worker_pid`,
  consumed by whichever accounting path resolves the death). This
  survives a dispatcher restart between signal delivery and reap.

## Durable timeout-kill intent

Table: `kanban_timeout_kill_intents(task_id, run_id, worker_pid, signal,
created_at, consumed_at)`. `persist_timeout_kill_intent()` inserts a row
immediately before `enforce_max_runtime` calls `kill()` (once for the
SIGTERM, again if the SIGKILL grace period is hit). `_classify_dead_worker`
checks `has_pending_timeout_kill_intent()` for the pid before consulting
`classify_infra_exit()`; when true it treats the death as
`dispatcher_killed=True` and consumes the intent
(`consume_timeout_kill_intent()`), so a second observation of the same
pid can never double-count. `clear_consumed_timeout_kill_intents()`
garbage-collects consumed rows older than 24h.

## Persistent interruption streak

Table: `kanban_interruption_streaks(task_id PRIMARY KEY, streak,
last_interrupted_at, reset_at, created_at)`. Every `infra` classification
(external allowed signal, startup-window dead pid, or quota signature —
including a quota signature with no usable retry-after) calls
`increment_interruption_streak()`. When the returned streak exceeds
`kanban.max_infra_interruptions` (default 3, minimum effective value 1 —
values `<= 0` are ignored and the default applies), `_account_infra_deaths`
force-trips `_record_task_failure` exactly like a normal crash, so the
circuit breaker still eventually applies. The streak is **preserved**
(not reset) by this promotion — only two paths reset it:

* `complete_task()` — a genuine non-interruption terminal outcome.
* `unblock_task()` — an explicit operator unblock.

A bare redispatch (reclaim → re-`ready` → re-claim) never resets it,
by design: that is exactly the loop the streak exists to bound.

## Provider backoff with a cap

Table: `kanban_provider_backoff(provider PRIMARY KEY, until, reason,
task_id, created_at)`. On a quota-signature `infra` death,
`_task_provider()` resolves the task's explicit `provider_override` (or
its profile's configured `agent.provider`) — `provider: auto` is
deliberately left unresolved (returns `None`) because auto-routing can
choose a healthy provider and must not be treated as exhausted. When a
provider resolves AND the log's `retry after Ns` parses to a usable
value, `register_provider_backoff()` parks the task in `scheduled`
(instead of `ready`) and every other task pinned to that provider is
guarded by `check_respawn_guard()` returning `"provider_backoff"` until
the pause elapses. `release_expired_provider_backoffs()` runs at the top
of every dispatch tick, resuming parked tasks and clearing expired rows
— durable across dispatcher restarts.

`retry-after` parsing (`_parse_retry_after`) accepts ONLY a positive
base-10 integer string; anything else (missing, non-numeric, `0`,
negative, a decimal) returns `None`. A value above
`kanban.provider_backoff_max_seconds` (default `86400`, i.e. 24h) is
clamped to the cap via `_clamp_retry_after`, which also returns an
operator-visible diagnostic string. **A malformed/missing/nonpositive
retry-after does NOT create a provider pause** — the death still counts
as `infra` (bumping the interruption streak) but never parks a task in
`scheduled`, so a quota signature with no usable backoff cannot loop the
dispatcher indefinitely without ever reaching the interruption cap.

## Config keys

* `kanban.count_infra_failures` (bool, default `false`) — when `true`,
  every `infra` classification is instead counted as `legit`.
* `kanban.infra_startup_window_seconds` (int, default `120`) — how long
  after this dispatcher process started a `pid N not alive` discovery
  is presumed to be a gateway restart / VM boot rather than a genuine
  crash. Overridable via `HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS`.
* `kanban.max_infra_interruptions` (int, default `3`, minimum effective
  `1`) — consecutive infra classifications allowed before a task is
  routed through normal counted-failure accounting. Overridable via
  `HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS`.
* `kanban.provider_backoff` (bool, default `true`) — provider-wide
  quota-parking. `false` disables parking entirely (quota deaths still
  classify as `infra` and bump the interruption streak, they just never
  reach `scheduled`). Overridable via `HERMES_KANBAN_PROVIDER_BACKOFF`.
* `kanban.provider_backoff_max_seconds` (int, default `86400`) — cap on
  a single provider pause; larger `retry after Ns` values are clamped.

## Upstream-ability

This is generic dispatcher behaviour with no fork-specific dependency —
worth offering upstream once landed and soaked here.
