# Evidence — t_023d0c6a: configured `smart` approval mode started as `manual`

Reproduction and verification scripts for commit `c337c6b9e6`. Kept so a
reviewer can re-derive the finding independently instead of trusting the
card narrative.

Both scripts are standalone and **read-only with respect to real config**.
Run them with the dev venv from the repo root:

```
/home/hermes/projects/hermes/hermes-agent-dev/.venv/bin/python \
  docs/evidence/t_023d0c6a/<script>.py
```

## `repro_approval_mode4.py` — the decisive reproduction

Builds a throwaway `HERMES_HOME` in `/tmp` with the layout that triggers the
bug: a launch profile set to `manual` and a named profile `work` configured
`smart`. Writes only inside that temp dir and removes it on exit.

Before the fix:

```
launch  _get_approval_mode() -> manual
work    _get_approval_mode() -> smart          <-- configured smart

config.get{key:approvals.mode}                -> {'value': 'manual'}
config.get{key:approvals.mode, profile:work}  -> {'value': 'manual'}   <-- profile ignored

info['profile_name']  -> work
info['approval_mode'] -> manual                <-- 'work' config says smart
```

The last pair is the core evidence: one `session.info` payload naming one
profile while reporting another profile's mode.

After the fix, both scoped reads return `smart` and the payload is
self-consistent.

Note the single-profile case was ALWAYS correct — that is why this needs a
two-profile layout to reproduce at all, and why a single-profile unit test
would have kept passing throughout.

## `verify_real_profiles.py` — verification against real profiles

The card required demonstrating the fix against a real configured `smart`
profile, not only a mock, because config propagation is exactly what mocks
hide. This script is strictly read-only: it never writes to any real config,
only exercising `config.get` and `_session_info` against the real profile
homes under `/home/hermes/.hermes`.

Current output (launch profile has no approvals block; all five named
profiles are configured `smart`). The roster changes as profiles are added or
renamed — what matters is the invariant, not these names:

```
claudecode     config.yaml='smart'  config.get='smart'  session.info='smart'  profile_name='claudecode'    OK
claudeprimary  config.yaml='smart'  config.get='smart'  session.info='smart'  profile_name='claudeprimary' OK
debugger       config.yaml='smart'  config.get='smart'  session.info='smart'  profile_name='debugger'      OK
orchestrator   config.yaml='smart'  config.get='smart'  session.info='smart'  profile_name='orchestrator'  OK
reviewer       config.yaml='smart'  config.get='smart'  session.info='smart'  profile_name='reviewer'      OK

ALL REAL PROFILES CONSISTENT: True
```

It asserts the invariant the bug violated: for every profile,
`config.yaml == config.get == session.info`, and `profile_name` names that
same profile.

## Root cause, in one paragraph

Approval mode is profile-scoped config, but one backend serves every profile
in the Desktop's app-global remote mode, so the ambient `HERMES_HOME` is the
LAUNCH profile's. Three gateway call sites resolved the mode against that
ambient home instead of the profile they were answering for. The resolver
itself was never wrong: `_load_approval_mode` already delegated to the
canonical `_get_approval_mode` (then in `tools/approval.py`; now
`tools/approval_context.py`). What was missing was the profile BINDING, now an
explicit parameter rather than ambient state.

## Upstream duplicate check — run, not a duplicate

Ran against `NousResearch/hermes-agent` across issues, PRs, and code, and
confirmed against the fetched `upstream/main` ref rather than the search index
(the tracker lags the code). Upstream still carries both defects this fixes:

```
tui_gateway/methods_config.py:213  "approval_mode": ... _load_approval_mode()   <- unscoped
tui_gateway/server.py:1673         def _load_approval_mode() -> str            <- takes no profile
tui_gateway/server.py:2052         yolo, approval_mode = False, "manual"       <- guessed seed
```

PR #95823 (`fix(tui_gateway): scope config.get/set RPC to params.profile`)
reached the same diagnosis independently from the Desktop-settings angle, but
is **CLOSED unmerged** and never touched `_session_info`, the seed, or the
renderer store — so it would not have fixed this card's symptom.

An earlier run recorded this check as impossible because `gh` looked
unauthenticated. That was a false negative from a worker's scratch environment;
the host credential was fine. Verify `gh auth status` before reporting an auth
wall.

## A note on drift

`_get_approval_mode` moved from `tools/approval.py` to
`tools/approval_context.py` in a later decomposition. Production
(`_load_approval_mode`) follows it correctly; the repro imports it with a
fallback so it keeps working on both trees.
