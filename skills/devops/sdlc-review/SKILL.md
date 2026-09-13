---
name: sdlc-review
description: Review Kanban handoffs and route verified outcomes.
version: 1.1.0
author: Jakub Wolniewicz (@frizikk) + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, review, quality, verification]
    category: devops
    requires_toolsets: [kanban]
environments:
  - kanban
---

# SDLC Review Skill

Independently verify work handed from a Kanban implementation run to the review lane, then approve it, request changes, or escalate. This skill reviews the deliverable and its evidence; it does not take over the implementer's work.

## When to Use

Use this skill when the dispatcher explicitly loads it for either review path:

- same-card review: the task was claimed from `review` after an implementer submitted a `review_requested` handoff; or
- ready-child review: a separate review card was claimed from `ready` with this skill forced and its parent handoff identifies the deliverable.

Both paths use the packet's effective review contract and exact round/cap metadata. A ready-child reviewer never edits the implementation: create a separately assigned repair card, link that repair ahead of the unfinished review, then call `kanban_request_changes` on the review child so its structured blocker contract and round are persisted. The review child waits on the repair and auto-resumes for independent re-verification when the repair completes.

## Prerequisites

- A Kanban worker context with the current task and run identifiers.
- Native Kanban tools: `kanban_show`, `kanban_comment`, `kanban_complete`, `kanban_request_changes`, and `kanban_block`.
- Workspace access through `read_file`, `search_files`, and `terminal` when the deliverable is code.
- The task's original specification, acceptance criteria, handoff summary, and prior run history must be available through `kanban_show`.

## How to Run

This skill is loaded automatically by the review dispatcher. Start with `kanban_show` before inspecting files or choosing a verdict.

1. Read the task specification and the latest `review_requested` handoff.
2. Inspect the actual deliverable and run relevant verification.
3. Choose exactly one verdict: approve, request changes, or escalate.
4. Record concrete evidence in the terminal Kanban transition.

## Quick Reference

| Verdict | When | Final action |
|---|---|---|
| Approve | Acceptance criteria and verification pass | `kanban_complete` |
| Request changes (same-card) | Correctable implementation defects remain | `kanban_request_changes` with all structured blockers |
| Repair (ready-child) | Correctable defects remain | Create a separately assigned repair, link it ahead of the review, then persist the verdict with `kanban_request_changes` |
| Escalate | A human decision or external prerequisite is required | `kanban_block` |

A same-card requested-changes transition returns the task to its original implementer. When that implementer requests review again without naming a reviewer, persisted provenance routes re-review to the same reviewer profile. A ready-child verdict instead leaves the review reviewer-owned and dependency-waiting on its separately assigned repair.

## Review Lenses

Vary how you look at the work on each round instead of repeating the same inspection. Decorrelated lenses catch different defect classes: a cold read of the artifact surfaces design and correctness problems that the implementer's narrative would have framed away, execution surfaces claims that do not reproduce, and a strict contract audit surfaces quiet scope drift. Repeating the round-1 lens on a later round mostly re-finds what round 1 already found.

Use the packet's `current_round` and `max_rounds` as authoritative. The
`changes_requested` entries in "Prior attempts on this task" remain an audit
trail, not a counter to reconstruct: do not derive the round from prose or
hardcode a terminal round. The configured cap may differ between boards or
deployments.

| Round | Lens | How to apply it |
|---|---|---|
| 1 | Artifact | Read the diff or deliverable cold, before the implementer's summary. Form an independent judgment, then compare it against the handoff narrative and investigate every mismatch. |
| Intermediate re-review | Execution | When `max_rounds == 0` or `current_round < max_rounds`, check out the work and actually run it via `terminal`: build, test, and exercise the reported behavior yourself. Verify each handoff claim empirically instead of re-reading the artifact. |
| Terminal bounded round | Contract | When `max_rounds > 0` and `current_round >= max_rounds`, re-read the ORIGINAL task body and acceptance criteria, audit the deliverable strictly against them, and verify every prior blocker landed. This is round 2 when the cap is 2; it is not hardcoded as round 3. Do not auto-approve unmet criteria: request changes normally and let the cap enforce its existing block. A comment or unblock without an authorized repair transition does not reset the contract or bypass the cap. |

The baseline duties in the Procedure section still apply on every round; the lens sets which inspection you lead with and weight most heavily.

On re-review, start from the delta since the last reviewed handoff plus the
unresolved findings. Use the prior and current commit IDs from structured
metadata when available. Re-run checks that cover changed or previously failing
paths, then smoke-test behavior that had already passed. Do not pay to repeat an
unchanged full-suite run unless the new delta can affect it or this card is the
graph's designated integration gate.

### Lens variation for ad-hoc review fan-outs

The same principle applies outside the Kanban review lane. When spawning multiple parallel reviewers via `delegate_task`, give each reviewer a different lens — one diff-only brief, one full-context brief, one checkout-and-run brief — rather than identical briefs. Identical briefs produce correlated verdicts and duplicate findings; varied briefs cover more defect classes for the same review spend.

## Procedure

### 1. Orient from the durable task record

Call `kanban_show` and identify:

- the original task body and acceptance criteria;
- the latest implementation summary and structured metadata;
- changed files, commit identifiers, and test evidence;
- comments and decisions from earlier runs;
- findings from prior review rounds.

Treat the handoff as a claim to verify, not as proof that the work is correct.

### 2. Compare requested behavior with delivered behavior

Map every acceptance criterion to concrete implementation or output evidence. Note omissions, changed semantics, and unrelated scope before deciding whether to run deeper checks.

For code work:

1. Use `read_file` and `search_files` to inspect the changed paths and their callers.
2. Use `terminal` to inspect the diff and run the project's existing focused tests, lint, type checks, or build commands.
3. Exercise the reported failure path and at least one ordinary control path when practical.
4. Check error handling, edge cases, concurrency boundaries, data preservation, security boundaries, and cross-platform behavior relevant to the change.
5. Confirm that tests assert behavior rather than merely snapshotting source text or constants.

Use staged verification ownership rather than duplicating every gate in every
lane:

- the implementer owns focused tests, affected lint/type checks, and direct
  behavior proof;
- the reviewer validates that evidence and independently re-runs checks selected
  from the diff's risk surface;
- a pre-created integration/release child owns the full applicable suite on the
  combined branch.

If no integration child exists and the acceptance criteria require a full
suite, run it here. Evidence is reusable input, never a substitute for an
independent risk judgment.

For non-code work:

1. Inspect the complete deliverable rather than only its summary.
2. Check correctness, completeness, formatting, and provenance.
3. Validate referenced URLs or external facts with the appropriate native tools when they affect the verdict.

### 3. Choose one verdict

#### Approve

Approve only when the acceptance criteria are satisfied and the evidence is sufficient. Call:

```text
kanban_complete(
    summary="Reviewed and approved. <what was verified>",
    metadata={"review_outcome": "approved", "reviewer_checks": [...]}
)
```

Include the exact checks that passed and any bounded caveat that does not block acceptance.

#### Request changes

Use this for specific, correctable defects. First record actionable findings:

```text
kanban_comment(
    task_id="<current-task-id>",
    body="Changes requested:\n1. <file or artifact + defect>\n2. <required correction>",
)
```

Then return the same task to its implementer with one consolidated verdict.
Each blocker needs a precise `reference` and one `basis`: `original_ac`,
`required_behavior`, `base_regression`, or `landing_gate`. On re-review, cite
an established reference or use `base_regression` with `rework_of` naming one:

```text
kanban_request_changes(
    reason="<concise summary of the required corrections>",
    blockers=[
        {"basis": "original_ac", "reference": "<AC + exact defect>"},
    ],
    followups=["<optional non-blocking improvement>"],
)
```

State where the defect is, how it reproduces, why it violates the task, and what minimum outcome would resolve it. Followups are inert and never release work. For a ready-child review, create a separately assigned repair card, link it ahead of the unfinished review, then call `kanban_request_changes` on the review child. That transition persists and validates the same blocker contract and round as same-card review, leaves the reviewer-owned review waiting on the repair dependency, and auto-resumes it for independent re-verification after the repair completes. Never edit the implementation as the reviewer or gate the repair behind the unfinished review/release.

#### Escalate

Use escalation only when the reviewer and implementer cannot resolve the problem without a human decision or external prerequisite:

```text
kanban_block(
    reason="escalation: <decision or prerequisite required>"
)
```

Explain the blocked decision and the smallest information needed to continue.

### 4. Preserve role separation

Do not edit the implementation while acting as reviewer. Request changes and let the implementer produce the next candidate; then independently verify that candidate in the next review run.

## Pitfalls

- **Rubber-stamping:** A passing handoff summary is not independent evidence.
- **Reviewer implementation:** Editing the deliverable hides ownership and weakens the re-review boundary.
- **Vague findings:** “Needs work” does not give the implementer a reproducible correction target.
- **Style-only blocking:** Do not request changes for preference-level nits when behavior and repository standards are satisfied.
- **Skipping prior rounds:** Re-review must confirm both the requested corrections and preservation of previously passing behavior.
- **Using blockers for ordinary rework:** Correctable defects belong in `kanban_request_changes`; reserve `kanban_block` for genuine external blockers or human decisions.
- **Completing without evidence:** Every approval summary must name the checks or artifacts actually inspected.

## Verification

Before submitting the verdict, confirm:

- [ ] `kanban_show` was read for the current task and run.
- [ ] Every acceptance criterion was mapped to evidence.
- [ ] The actual deliverable was inspected.
- [ ] Relevant focused checks were run or an explicit reason was recorded when execution was impossible.
- [ ] Prior requested changes were re-tested on re-review.
- [ ] Unrelated regressions and scope changes were considered.
- [ ] The verdict uses exactly one terminal action.
- [ ] The summary contains concrete, non-secret evidence.
- [ ] No implementation files were edited by the reviewer.
