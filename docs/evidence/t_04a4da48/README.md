# Evidence — t_04a4da48 (ROADMAP Phase 2.2, re-file of the archived t_023d0c6a)

## TL;DR

The originally-reported symptom **no longer reproduces on `dev`**. It was
root-caused and fixed on the archived card `t_023d0c6a`, and that fix is
merged into `dev` (`c337c6b9e6`, evidence `2178a37ba9`, both ancestors of
`dev`). This card re-verifies that on current `dev` and fixes the one
remaining layer the earlier fix did not reach: the Desktop renderer's
`applyRuntimeInfo` path.

## Reproduction — `repro_phase22.py`

Runs in an isolated development `HERMES_HOME` under `~/.hermes-dev/t_04a4da48`
(never the managed `~/.hermes` runtime). Records the three artifacts the card
requires — the resolved configuration, the initial session state, and the
visible Desktop mode — across four profile layouts.

```
/home/hermes/projects/hermes/hermes-agent-dev/.venv/bin/python \
  docs/evidence/t_04a4da48/repro_phase22.py
```

Output on `dev` @ `1f772f1305`:

```
=== A_no_block_named_smart ===        (this VM's real layout)
  raw_launch_yaml_approvals        None
  merged_launch_approvals_mode     smart
  resolver_launch                  smart
  raw_named_yaml_approvals         {'mode': 'smart'}
  resolver_named                   smart
  config_get_unscoped              {'value': 'smart'}
  config_get_named                 {'value': 'smart'}
  session_info_launch              {'profile_name': 'default', 'approval_mode': 'smart', 'yolo': False}
  session_info_named               {'profile_name': 'work',    'approval_mode': 'smart', 'yolo': False}

=== D_manual_vs_smart ===             (the two-profile shape that used to fail)
  resolver_launch                  manual
  resolver_named                   smart
  config_get_unscoped              {'value': 'manual'}
  config_get_named                 {'value': 'smart'}     <-- was 'manual' before c337c6b9e6
  session_info_launch              {'profile_name': 'default', 'approval_mode': 'manual'}
  session_info_named               {'profile_name': 'work',    'approval_mode': 'smart'}
```

Every payload is now self-consistent: `approval_mode` and `profile_name`
describe the same profile at every rung. Scenario D is the exact layout that
produced `profile_name: 'work'` alongside `approval_mode: 'manual'` before the
fix landed.

## What the initial mode is SUPPOSED to be

The card calls this ambiguous. It is not — it is just undocumented in the
place people look. The shipped default is **`smart`**:

- `hermes_cli/config_defaults.py:1512` — `"approvals": {"mode": "smart", ...}`
- A profile with **no** `approvals` block therefore resolves to `smart`, not
  `manual` (scenario C above: `raw=None` → `merged=smart`).
- `manual` is the **fail-safe**, not the default. `_normalize_approval_mode`
  (`tools/approval_context.py:200-214`) returns `manual` for an unrecognized
  value and warns; the renderer's `normalizeApprovalMode`
  (`apps/desktop/src/store/approval-mode.ts:23-29`) mirrors that. So "starts in
  manual" is always the signature of a value that failed to resolve — never of
  a default being applied.
- The renderer optimistically labels an unread profile `smart`
  (`approval-mode.ts:32`) precisely because that is the shipped default.

The naming confusion the card flags (`grep smart agent/approval*.py` finds
nothing) is because the approval code lives in **`tools/`**, not `agent/`:
`tools/approval.py`, `tools/approval_context.py` (the resolver + the
`_VALID_MODES = ("manual", "smart", "off")` tuple), `tools/approval_smart.py`
(the guardian), `tools/approval_prompt.py`.

## Precedence chain, as it stands on `dev`

| Layer | Owner | Status |
|---|---|---|
| profile config | `~/.hermes/profiles/<p>/config.yaml` → `DEFAULT_CONFIG` | authoritative |
| resolver | `tools/approval_context.py:228 _get_approval_mode()` | correct; single owner |
| persisted session state | *(none — deliberately)* | mode is profile config, not conversation state (`hermes_cli/approval_mode.py:3-6`) |
| gateway binding | `tui_gateway/server.py:1677 _load_approval_mode(profile_home)` | correct since `c337c6b9e6` |
| gateway read/write | `methods_config.py:215-216`, `server.py:2077` | profile-bound |
| Desktop statusbar sync | `store/approval-mode.ts:50 syncApprovalModeForProfile` | sends the profile it caches under |
| Desktop event stream | `use-message-stream/gateway-event/session-info.ts:189` | gated on `event.profile` + active source |
| **Desktop `applyRuntimeInfo`** | `use-session-actions/utils.ts:1736` | **was still wrong — fixed on this card** |

## The residual defect this card fixes

`applyRuntimeInfo` reconciled the mode against `$activeGatewayProfile.get()` —
the *ambient* active profile — instead of the profile the payload names, and
did so even for background tiles (`{ foreground: false }`).

The backend has stamped `profile_name` into every `_session_info` payload since
`c337c6b9e6` (`tui_gateway/server.py:2105`), but
`SessionRuntimeInfo` in `apps/desktop/src/types/hermes.ts` never declared the
field, so the renderer discarded it and substituted the ambient guess.

That is the same cross-profile bug the gateway fix removed, reintroduced one
layer up. It fires on the paths that pass `foreground: false`:

- `use-session-actions/index.ts:835` — a Project "+" tile
- `use-session-actions/index.ts:2193` — a branched session

Both run in their own profile, and both were writing that profile's mode into
whichever profile's statusbar happened to be active. The sibling event path
(`session-info.ts:189`) already guarded exactly this with
`isActiveEvent && event.profile && fromActiveSource()`; `applyRuntimeInfo` had
no equivalent guard.

**Safety note:** this narrows attribution, it never widens what auto-approves.
It cannot cause a profile configured `manual` to be treated as `smart` — the
enforcement path (`tools/approval.py:985`, `:1063`) reads the resolver
directly and never consults this renderer cache. The cache is presentation
only. No approval gate is weakened, so the card's stop-and-block guardrail
does not fire.
