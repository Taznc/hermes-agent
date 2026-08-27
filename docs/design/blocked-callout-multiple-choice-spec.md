# Blocked-Callout Multiple-Choice Questions — Spec

Status: design proposal (not yet implemented)
Author: drafted by claudeprimary for t_7f8aa16a ("Kanban User input Questions")
Consumers: t_534ff6f7 (backend response wiring), t_88d200d7 (frontend rendering)

## Problem

The blocked-callout `CtaBanner` (`apps/desktop/src/plugins/kanban/drawer.tsx`)
renders a `needs_input` block's `reason` as a single opaque paragraph, with a
free-text `CommentComposer` as the only way to answer. When a worker's
question is actually a closed set of choices (as in t_7f8aa16a's A/B/C
example), the user has to retype one of the options as prose, and downstream
automation has to re-parse that prose to recover the decision. We want the
option set to render as clickable controls, and the click to produce a
structured, machine-parseable response — without adding new core tool
surface or a new blocking RPC shape (narrow-waist rule in `AGENTS.md`).

## Design summary

1. A worker embeds an optional **fenced `choices` block** inside the existing
   free-text `reason` string it already passes to `kanban_block`. No tool
   schema change, no new column on `tasks`.
2. The dashboard API's comment endpoint gains one new **optional** field,
   `choice`, alongside the existing `body`/`author`. A user's click POSTs a
   comment carrying both a human-readable `body` (so the activity stream
   reads the same as any other reply) and the structured `choice` (so
   automation doesn't have to parse prose).
3. The frontend parses `reason` once, per render, with a strict validator.
   Anything that isn't a clean, small, well-formed option list falls back to
   today's plain-text banner + free-text composer, unchanged. Malformed
   input never crashes the drawer and never silently guesses.

## 1. Representing options in the task/comment payload (ask side)

### Where it lives

Workers already call `kanban_block(reason=<str>, kind="needs_input")`. The
`reason` string is stored verbatim in the `blocked` event payload (see
`hermes_cli/kanban_db.py::block_task`, `_append_event(..., "blocked",
{"reason": reason, ...})`) and is what `CtaBanner` renders today via
`latestBlockReason(events)`.

We do **not** add a new parameter to `kanban_block` or a new DB column. That
would touch the model-tool surface every worker sees on every call, for a
UI-only concern. Instead, a worker that wants structured options appends a
fenced code block to the same `reason` string it already writes, using the
info-string `choices`:

    Task premise is false: ...three-way human decision needed...

    ```choices
    [
      {"key": "A", "label": "PR the commits upstream to NousResearch/hermes-agent main"},
      {"key": "B", "label": "Repoint the managed runtime's origin/branch at the fork's dev branch"},
      {"key": "C", "label": "Authorize a one-time manual cherry-pick"}
    ]
    ```

Everything before the fence is the question prose (still shown verbatim,
unchanged, above the options). The fence's body is a JSON array of option
objects. This is additive and 100% backward compatible: every `reason`
string ever written by an existing worker has no ` ```choices ` fence, so it
renders exactly as it does today (see "Fallback" below).

### Wire shape

```ts
interface BlockedChoiceOption {
  /** Stable short key, unique within the question. Rendered as the button's
   *  accessible prefix ("A", "B", "C", ...) but not required to be a single
   *  letter — a worker MAY use semantic keys ("approve", "reject"). */
  key: string
  /** Human-readable option text, rendered as the button's label. */
  label: string
  /** Optional longer description shown as secondary/muted text under the
   *  label (e.g. the one-line consequence of picking this option). */
  description?: string
}
```

Parsing contract (`parseBlockedChoices(reason: string)` — new pure function,
colocated with `latestBlockReason` in `drawer.tsx`):

1. Find the **last** ` ```choices ... ``` ` fence in the string (a worker
   could quote an example fence earlier in its prose; the last one wins,
   mirroring how models append the "real" structured payload at the end).
2. `JSON.parse` the fenced body. Any parse error → treat the question as
   free-text (no fence found).
3. Validate the parsed value against the option-list rules below. Any
   validation failure → treat as free-text and additionally surface a small
   dev-only `console.warn` (never a user-facing error toast — a malformed
   question is a worker bug, not something the user caused).
4. The question **prose** shown to the user is `reason` with the fence
   (including the ` ```choices `/` ``` ` delimiters) stripped and trailing
   whitespace trimmed.

### Option-list validation rules (malformed-input contract)

An options array is **valid** iff all of the following hold; otherwise the
whole question falls back to free-text (no partial rendering of "some"
options):

- It parses as JSON and is an `Array`.
- Length is between 2 and 6 inclusive. (1 "option" isn't a choice; more than
  6 stops being a scannable click UI — a worker with more than 6 outcomes
  should ask a narrower question or fall back to free text itself.)
- Every element is an object with a non-empty string `key` and non-empty
  string `label`. `description`, if present, must be a string.
- `key` values are unique (case-sensitive) within the array.
- Total serialized size of the fence body is ≤ 4 KB (guards against a
  runaway/degenerate payload; this is a UI parse limit, not a new stored
  limit).

Any single violation invalidates the whole set — we never render 2 good
options and silently drop a malformed 3rd, because that changes what the
question means without telling anyone.

## 2. Click → structured response (answer side)

### API change

`POST /tasks/{task_id}/comments` (`plugins/kanban/dashboard/plugin_api.py`)
gains one new optional field on `CommentBody`:

```python
class ChoiceResponse(BaseModel):
    key: str
    label: str            # snapshot of the label at click time
    question_event_id: int  # id of the `blocked`/`block_loop_detected` event this answers

class CommentBody(BaseModel):
    body: str
    author: Optional[str] = "dashboard"
    choice: Optional[ChoiceResponse] = None
```

`body` is still always sent and is still what renders in the plain comment
stream (e.g. `"B) Repoint the managed runtime's origin/branch at the fork's
dev branch"`), so the activity feed and any code that reads `comment.body`
keeps working unmodified — this is why the wire mechanism reuses "the
existing task-comment/response mechanism" the task body calls for, rather
than inventing a parallel response channel.

`choice`, when present, is persisted as a new nullable `choice_json` column
on `task_comments` (same additive-migration pattern as `tasks.block_kind`:
`_add_column_if_missing(conn, "task_comments", "choice_json", "choice_json
TEXT")`), stored as the serialized `ChoiceResponse`. `GET` endpoints that
return comments (`_comment_dict`) add `choice: dict | null` alongside the
existing fields — additive, so existing frontend readers ignore it.

### Click → request mapping

1. User clicks option `B` in the rendered choice list.
2. Frontend immediately (optimistic) marks that option `selected` (see UI
   states below) and disables the other options.
3. Frontend calls the existing comment-add mutation with:
   ```json
   {
     "body": "B) Repoint the managed runtime's origin/branch at the fork's dev branch",
     "author": "<current user/dashboard identity, same as free-text replies>",
     "choice": { "key": "B", "label": "Repoint the managed runtime's origin/branch at the fork's dev branch", "question_event_id": 4821 }
   }
   ```
4. On success, the option list transitions to `submitted` (locked, chosen
   option visually marked, no further clicks) — it does **not** wait for the
   task to actually unblock; submitting the answer and unblocking the task
   are separate actions today (`ctaReply` vs `ctaUnblock` buttons) and this
   spec does not change that split. The existing "Unblock" button remains
   available and unaffected.
5. On failure (network/5xx), the option list reverts to `unanswered` and an
   inline error shows under the options (see Error handling), matching the
   existing "be optimistic, then honest" rule from the desktop AGENTS.md.

`question_event_id` is what lets a downstream consumer (a worker resuming
after unblock, or an audit trail) unambiguously bind an answer to the
question it answers, even if the task was blocked/unblocked/re-blocked
multiple times with different option sets — matching how `latestBlockReason`
already keys off the specific `blocked` event.

## 3. UI states

Rendered in place of today's plain `<p>{reason}</p>` inside `CtaBanner`,
only when `parseBlockedChoices` returns a valid option list; otherwise the
existing paragraph + `CommentComposer` render exactly as today.

| State | Trigger | Appearance | Interaction |
|---|---|---|---|
| `unanswered` | Valid choice list parsed, no comment with matching `question_event_id` exists yet | Question prose, then each option as a full-width outline button in document order (`A`, `B`, `C`, ...), each showing its `key` badge + `label` + optional `description` | All options clickable/focusable; `Reply`/free-text affordance is NOT shown (options ARE the reply) |
| `selecting` (optimistic, in-flight) | User clicked an option, POST not yet resolved | Clicked option shows a filled/selected treatment + inline spinner; all other options `disabled` (dimmed, non-interactive) | No further clicks accepted until the request settles |
| `submitted` | POST succeeded, OR a prior comment with a `choice` matching this `question_event_id` already exists (e.g. re-opening the drawer after answering) | Chosen option stays filled/selected with a check icon; unselected options render dimmed/disabled, not hidden (so the user can see what they didn't pick) | Read-only. The existing `Unblock` action in the CTA banner is unaffected and still lets the human requeue the task |
| `error` | POST failed | Reverts to `unanswered` visual state, plus a small inline `text-[0.75rem]` error line under the options (reuses the tone/copy pattern of other inline mutation errors in `drawer.tsx`) and a `Retry` affordance (re-clicking the same option retries) | Options re-enabled |

Multiple `blocked` events can accumulate on one task (e.g. block → unblock →
re-block). Only the **latest** `blocked`/`block_loop_detected` event's
options are rendered in the CTA banner (matching current `latestBlockReason`
behavior, which already only surfaces the latest one). Earlier answered
questions remain visible read-only in the comment/activity thread below, as
plain comments — nothing new needed there since `body` is always populated.

## 4. Accessibility / keyboard requirements

The option list is a single-select group and must use native
[ARIA radiogroup](https://www.w3.org/WAI/ARIA/apg/patterns/radio/) semantics,
not a set of independent buttons:

- Container: `role="radiogroup"`, `aria-label` = the question prose (or a
  generic fallback like "Choose an option" if the prose is empty after
  stripping the fence).
- Each option: `role="radio"`, `aria-checked` reflecting `unanswered` (all
  false) / `selecting`+`submitted` (true on the chosen one, false on others).
- Exactly one option is in the natural tab order at a time (`tabIndex=0` on
  the checked option, or the first option when none is checked yet);
  `tabIndex=-1` on the rest — standard roving-tabindex radiogroup behavior.
- `ArrowDown`/`ArrowRight` moves focus + selection to the next option,
  `ArrowUp`/`ArrowLeft` to the previous, wrapping at the ends; `Home`/`End`
  jump to first/last.
- `Enter` or `Space` on a focused option submits it (same action as a
  pointer click — both funnel into the one submit handler).
- In `submitted`/`error`-being-retried states, disabled options get
  `aria-disabled="true"` (not removed from the DOM) and are skipped by arrow
  navigation.
- Focus must be visible (reuse the app's existing focus-ring token; do not
  suppress `:focus-visible`).
- No color-only signal: the checked state is also conveyed by a check icon
  and `aria-checked`, not tone alone (existing `Codicon name="check"` pattern
  already used elsewhere in this file for the analogous "current assignee"
  checkmark).

## 5. Fallback when a question has no discrete options (free-text)

This is the default path and must not regress:

- `parseBlockedChoices(reason)` returns `null` whenever: no ` ```choices ` fence
  is present, the fence fails to parse as JSON, or the parsed value fails
  any validation rule in §1.
- When it returns `null`, `CtaBanner` renders **exactly what it renders
  today** — the full `reason` string as a plain paragraph, with the existing
  `Reply` button opening the existing free-text `CommentComposer`, and the
  existing `Unblock` button. No new component is mounted, so there is zero
  behavior change for every question asked before this feature exists.
- A free-text reply's comment POST omits `choice` entirely (`choice: null`),
  which is exactly what every existing comment payload already sends
  post-migration (the column is nullable, additive, and never required).

## 6. Error handling for malformed option lists (summary)

| Failure | Handling |
|---|---|
| No fence found | Not an error — free-text fallback (§5), silent |
| Fence body is not valid JSON | Free-text fallback; `console.warn('[kanban] malformed choices fence', err)` in dev builds only |
| Parsed value is not an array, or array length outside [2,6] | Free-text fallback; same dev warning, includes the actual length |
| Any element missing `key`/`label`, wrong type, or empty string | Free-text fallback; dev warning names the offending index |
| Duplicate `key` values | Free-text fallback; dev warning names the duplicated key |
| Fence body exceeds 4 KB | Free-text fallback; dev warning states the byte size |
| Comment POST with a `choice` fails validation server-side (e.g. `question_event_id` doesn't reference an existing event on this task) | `422` from the API; frontend surfaces the `error` UI state (§3) — the click is never silently dropped |
| Two options with identical `label` but different `key` | Allowed — `key` is the identity, `label` is display text only; not a malformed-list condition |

No malformed-input case should ever throw inside render or crash the drawer;
`parseBlockedChoices` is a pure function that always returns `T | null` and
is called inside existing render logic already guarded by the drawer's error
boundary as a last resort, not the first line of defense.

## Non-goals / open questions (explicitly out of scope for this spec)

- Multi-select (checkbox-style) questions — the task body and the concrete
  example are single-choice; add a separate `multi: true` flag later if a
  real use case appears (see AGENTS.md's stance against speculative
  surface).
- Changing `kanban_block`'s tool schema to accept options as a first-class
  parameter instead of a fenced convention in `reason` — left as a possible
  future follow-up if the fenced-text convention proves awkward for workers
  to produce reliably; not needed for this feature to ship.
- Re-rendering options after the task moves out of `blocked` (e.g. showing
  the answered question read-only on a `done`/`review` task's history) is
  free (comments already render in the activity thread), so it is not a new
  UI state, just existing comment rendering plus the `choice` field being
  available if a future pass wants to give it a distinct treatment.
