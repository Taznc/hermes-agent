# Infra-death classification for the kanban dispatcher

## Goal

The dispatcher's failure budget (`kanban.failure_limit` / `--max-retries`)
existed to stop the dispatcher thrashing forever on a task that keeps
failing for reasons the worker itself caused. It was never meant to
punish a task whose worker was killed by something *outside* the task —
a gateway restart, a VM reboot, or a provider quota wall. Measured on
this board (2026-09-05): 18 `gave_up` events fired on `pid N not alive`
during what were, on inspection, host/gateway restarts, and one task hit
its provider's 5,841-second quota window three minutes in and was
retried into a second `gave_up` before the window had a chance to clear.

This change adds an `infra` classification to the existing dispatcher
exit-reap path (`detect_crashed_workers` / `_classify_worker_exit` in
`hermes_cli/kanban_db.py`) so these deaths stop consuming the failure
budget, while everything that IS a genuine task failure — nonzero
exits, the dispatcher's own `--max-runtime` kill, iteration-budget
exhaustion — keeps counting exactly as it does today.

## Categories

| Category | Meaning | Failure budget | Event kind |
|---|---|---|---|
| `legit` | The task itself is at fault, or the dispatcher took a deliberate, accounted action (its own `--max-runtime` kill). | Counts (`consecutive_failures` += 1; may trip `gave_up`). | `crashed` / `timed_out` (unchanged) |
| `infra` | The worker died for a reason external to the task: an unrecognized SIGKILL/SIGTERM the dispatcher did not send itself, a dead PID discovered inside the dispatcher's own startup window (gateway restart / VM boot), or a provider 429/quota exit. | Does NOT count. Task re-queues to `ready` immediately. | `interrupted` |
| `unknown` (legacy) | Reap registry has no record and none of the infra signals matched — indistinguishable today from a genuine crash. | Counts (unchanged; this is the existing `unknown` → `crashed` path). | `crashed` |

`kanban.count_infra_failures: true` collapses `infra` back into `legit`
for every rule above (today's pre-existing behaviour), while the event
still records the underlying signal it detected for observability.

## Decision table

Evaluated by the new pure function `classify_infra_exit()`. Inputs are
facts the caller (`detect_crashed_workers`) gathers from existing and
new state; the function itself does no I/O.

| exit_kind (from `_classify_worker_exit`) | Extra signal | Category | reason |
|---|---|---|---|
| any | quota/429 signature found in the worker's final log lines | `infra` | `quota` |
| `signaled` | `dispatcher_killed` True (pid appears in the in-process kill-intent registry — set right before the dispatcher's own `enforce_max_runtime` / `_terminate_reclaimed_worker` sends SIGTERM/SIGKILL) | `legit` | `dispatcher_kill` |
| `signaled` | `dispatcher_killed` False | `infra` | `external_signal` |
| `unknown` (no reap record — the existing `pid N not alive` path) | this process was marked as a real dispatcher loop (`mark_dispatcher_process_started()`, called once by the gateway-embedded tick loop / `hermes kanban daemon --force`) less than `kanban.infra_startup_window_seconds` (default 120s) ago | `infra` | `startup_window` |
| `unknown` | not marked as a dispatcher loop, or marked more than the window ago | `legit` | `unknown_exit` (unchanged today) |
| `nonzero_exit` | none of the above | `legit` | `nonzero_exit` (unchanged today) |
| `clean_exit` / `rate_limited` | n/a — untouched, existing dedicated handling | (unchanged) | n/a |

Regression guards (explicit, tested):

* `nonzero_exit` NEVER becomes `infra` on its own (only an explicit quota
  log signature can override it — that is a real quota death that
  happened to exit nonzero, not a generic bug).
* Iteration-budget exhaustion (`Iteration budget exhausted (N/N)`) is
  recorded by `agent/turn_finalizer.py` calling `_record_task_failure`
  directly from inside the still-running worker, BEFORE the process
  exits — the task is no longer `status='running'` by the time
  `detect_crashed_workers` would ever see it, so this path is
  structurally untouched by this change. A regression test asserts the
  failure still counts.
* The dispatcher's own `--max-runtime` kill (`enforce_max_runtime`)
  keeps its own synchronous accounting (`timed_out` + `_record_task_failure`)
  exactly as today; the kill-intent registry additionally lets
  `detect_crashed_workers` recognize that pid as dispatcher-owned if the
  worker's death is observed on a *later* tick (e.g. dispatcher process
  restarted between sending the signal and recording the outcome).

## Config keys

* `kanban.count_infra_failures` (bool, default `false`) — when `true`,
  every `infra` classification above is instead counted as `legit`
  (restores pre-change behaviour). Documented alongside the other
  `kanban.*` keys in `website/docs/user-guide/features/kanban.md`.
* `kanban.infra_startup_window_seconds` (int, default `120`) — how long
  after this dispatcher process started a `pid N not alive` discovery
  is presumed to be a gateway restart / VM boot rather than a genuine
  crash. Also overridable via `HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS`
  for tests, mirroring `HERMES_KANBAN_CRASH_GRACE_SECONDS`.

## Upstream-ability

This is generic dispatcher behaviour with no fork-specific dependency —
worth offering upstream once landed and soaked here.
