# Desktop model recommendation gateway contract

`model_recommendation.get` is a profile-scoped JSON-RPC read operation for the Desktop composer. It is advisory only: it does not create or alter a session, model selection, reasoning effort, or provider configuration.

## Request

`profile` (optional) selects the profile whose router configuration and
provider inventory serve the request. Omitted, `null` or blank means the
gateway's launch profile, exactly as before; `"default"` (any casing) means
the default profile. An explicitly supplied profile that is a non-string,
fails profile-name validation, is unknown, or has been deleted is rejected
with JSON-RPC code `4002` BEFORE any router or provider call, and never falls
back to the launch profile: the unsent draft must not reach a router that a
different profile configured. The rejection message contains no draft text.

```json
{
  "profile": "work",
  "draft": "Complete unsent composer text",
  "attachments": [{"name": "brief.pdf", "mime_type": "application/pdf", "size": 42, "kind": "file"}],
  "policy": "balanced"
}
```

`policy` is one of:

- `balanced`: cheapest adequate eligible route.
- `save_codex`: preserves Codex capacity unless it is materially advantageous.
- `best_quality`: strongest eligible route with reasoning effort scaled to task risk.

Only complete unsent draft text and attachment metadata are accepted. Attachment bytes, local paths, prior transcript content, project files, secret values, and hidden context are neither accepted nor passed to the router.

## Response

A configured router returns:

```json
{
  "status": "ok",
  "policy": "balanced",
  "recommendations": [{
    "provider": "provider-slug",
    "model": "model-id",
    "effort": "medium",
    "reason": "Short explanation",
    "capabilities": {"reasoning": true, "fast": false, "effort_options": ["none", "low", "medium"]},
    "availability": {"status": "fresh", "allowed": true, "limit_reached": false}
  }]
}
```

There is at most one result per eligible configured provider. Results are ranked under the requested policy. Availability status is explicit: `fresh`, `stale`, `unavailable`, `failed`, or `unsupported`; stale and unavailable account data are never represented as fresh capacity.

An unavailable router, missing router configuration, malformed output, invalid input, or no eligible configured route produces either a JSON-RPC validation error (invalid input) or an explicit result:

```json
{"status": "unavailable", "reason": "...", "recommendations": []}
```

Older clients do not invoke this optional method and retain current composer behavior. Newer clients should treat an unknown-method response from an older backend as feature unavailable.

## Persisted composer preset (supplemental v1 contract)

This is the final settings seam for the Desktop consumer. It extends the existing
`config.get` / `config.set` allowlists with exactly one key,
`model_recommendation.preset`. It is not an auxiliary model-assignment slot.

```typescript
type RecommendationPreset = 'balanced' | 'save_codex' | 'best_quality';
interface PresetReadParams {
  key: 'model_recommendation.preset';
  profile: string;
}
interface PresetWriteParams extends PresetReadParams {
  value: RecommendationPreset;
}
interface PresetReadResult {
  value: RecommendationPreset;
  profile: string;
}
interface PresetWriteResult extends PresetReadResult {
  key: 'model_recommendation.preset';
}
```

Example JSON-RPC envelopes:

```json
{"jsonrpc":"2.0","id":1,"method":"config.get","params":{"key":"model_recommendation.preset","profile":"work"}}
{"jsonrpc":"2.0","id":1,"result":{"value":"balanced","profile":"work"}}
{"jsonrpc":"2.0","id":2,"method":"config.set","params":{"key":"model_recommendation.preset","profile":"work","value":"save_codex"}}
{"jsonrpc":"2.0","id":2,"result":{"key":"model_recommendation.preset","value":"save_codex","profile":"work"}}
```

Profile and failure rules:

- The settings calls REQUIRE an explicit, non-empty profile string, including
  `"default"` when that is the intended target. Names use existing profile
  normalization (trim/lowercase), validation and non-deleted-profile lookup;
  the response stamps the canonical name. Unknown or invalid names fail rather
  than falling back to the gateway's launch profile. A `session_id` never selects
  the settings profile and never causes session mutation.
- Writes accept exactly the three lower-case strings, without coercion or
  trimming. Missing/null/boolean/numeric/object values, unknown values and
  differently cased/padded strings fail with JSON-RPC code `4002`. No file is
  altered. Missing/non-string profile also produces `4002` on either call;
  empty/invalid/unknown string profiles produce `5001` on `config.get` (the
  existing getter-error envelope), `4002` on `config.set`.
- A missing file/key or invalid stored preset reads as `balanced` without
  repairing the file, seeding profile files or configuring a router. This
  includes null/non-mapping `model_recommendation` sections. A corrupt,
  non-mapping-root or unreadable YAML file instead returns `5001` with
  `Could not read model recommendation preset`; the response contains no
  config content, paths or credentials from the read failure.
- Writes use the selected profile's config file, a fresh single-key atomic YAML
  update and the existing config lock. Unrelated settings, comments, raw
  environment references and auxiliary assignments are preserved. A malformed
  file, non-mapping existing `model_recommendation` section, or I/O failure
  returns `5001` with `Could not save model recommendation preset`. There is no
  fallback write to a different profile. A successful write acknowledges only
  after persistence; a new read/reload sees it without restarting.
- Managed-scope pins on `model_recommendation.preset`, its parent
  `model_recommendation` section, or ANY descendant key beneath the preset
  (a pinned subtree such as `model_recommendation: {preset: {future: x}}`),
  and the existing package-managed config write-lock, reject saves before
  mutation with the same sanitized `5001` save error. Reads still use the
  effective gateway configuration, including managed values; a rejected save
  never silently persists an ineffective value.
- An existing literal top-level YAML key `model_recommendation.preset` also
  rejects saves before mutation with that `5001` save error, whether or not the
  nested `model_recommendation: {preset: ...}` setting exists. The literal key
  is not reinterpreted, migrated, deleted or overwritten.
- An anchored `model_recommendation` mapping, an anchored document root (any
  `&anchor` on the top-level mapping, whose aliases would carry the preset into
  sibling, nested, sequence or `<<` merge sites), or a target supplied by a
  root YAML `<<` merge, rejects saves before mutation with the same sanitized
  `5001` save error and leaves the file byte-for-byte unchanged. The round-trip
  writer would otherwise mutate shared alias/merge sources and unrelated
  effective settings. This guard conservatively rejects even an unreferenced
  anchor on the target or root mapping. Reads remain supported; unrelated
  anchors, aliases and merges (including a target that itself `<<`-merges an
  unrelated anchor) do not prevent saving an independent, explicitly defined
  target.
- Reads return only `value` and `profile`, never router configuration. Writes
  ignore unrelated request fields; only the preset is persisted. Do not send
  draft text or recommendation results to the settings methods.
- Older backends return `4002` (unknown config key) for this key; treat that as
  unavailable persistence, not permission to save locally or choose a model.
  Any failed save must remain visibly failed; do not claim it was persisted.

The recommendation operation itself is intentionally unchanged: an OMITTED
`policy` still resolves to `balanced`, NOT the persisted preset. Existing
explicit request-policy normalization also remains unchanged (trim/lowercase;
null and other falsy values resolve to `balanced`; invalid policy -> `4000`).
The Desktop flow is: read this profile's preset, show it before requesting,
persist deliberate selector changes, then send that displayed choice explicitly
as `policy` to `model_recommendation.get` with the SAME `profile`. Ignore late
responses belonging to another profile. This avoids changing behavior for v1
callers that previously omitted policy. Reading/writing the preset does not
invoke a router, apply a model, alter reasoning effort, or submit a draft.

Implementation ownership: `hermes_fork/model_recommendation/settings.py`,
registered through `hermes_fork/gateway.py`'s existing fork anchor. Original
approved backend commit: `499088dce2feb32f588397e157e5f3f91c772334`; local
provenance-preserving integration merge: `d42ac34c444aad7bea09825ef6ae73adddda07d1`.
The supplemental source commit is recorded in the review handoff; consumers
must integrate that branch, not only the original backend commit.

## Desktop attachment mapping and eligibility (v1 unchanged)

Construct a new allowlisted object per `ComposerAttachment`; never spread the
attachment object into the RPC payload:

- `label` -> `name` (only the human-visible label, not `path` or `detail`).
- `kind` -> `kind` (as supplied by the composer).
- Omit `mime_type` and `size` when unknown. The composer need not have those
  fields. Do not guess MIME type, stat a file, or read/download bytes to fill them.
- Do not include local paths, `detail`, `refText`, `previewUrl`, `thumbnailUrl`,
  attachment bytes, prior transcript/session content, project files, secret
  values or hidden context. Extra request fields are ignored; extra attachment
  keys are filtered before router invocation. Values in allowed fields are not
  content-redacted: the caller must not put a path or secret into `name`.

Eligibility and missing-value behavior:

- `draft` must be a string with at least one non-whitespace character and at most
  100,000 Unicode code points. Validation checks whitespace but forwards the
  COMPLETE original string, including surrounding whitespace. The renderer must
  never trim or truncate a draft to make it eligible. Count code points, not
  JavaScript UTF-16 code units (for example, `Array.from(draft).length`). Invalid
  draft input returns `4000` before a router can run.
- Omitted `attachments` means `[]`. For a configured router it must be a list of
  metadata objects, otherwise `4000`. The original backend caps processing at the
  FIRST 32 entries; this supplement does not change that v1 behavior. Therefore
  the Desktop must disable/explain recommendation when there are more than 32
  attachments, never silently drop entries or claim full attachment coverage.
- Each `name`, `kind` or known `mime_type` string is limited to 256 code points;
  a known `size` should be a non-negative integer, at most `2**53`. For
  compatibility the v1 filter accepts strings up to 256 code points or integers
  in `[0, 2**53]` for any of the four keys, but the Desktop should send only the
  conventional field types above. Missing/null/overlong/unsupported values are
  omitted, not guessed or truncated. An empty metadata object remains `{}`;
  an attachment with only name/kind is accepted and does not assert MIME/size.
- If an existing label/kind cannot fit the bounds, the Desktop should disable
  and explain rather than truncate it or silently omit an attachment. Unknown
  optional MIME/size does not make an otherwise eligible draft ineligible.
- The result is a draft-and-metadata-only assessment, not an inspection of the
  attachment content. Explain that distinction. No image/document bytes or
  session context are fetched to enrich a recommendation.

## Configuration

The profile-scoped defaults are inert:

```yaml
model_recommendation:
  preset: balanced
auxiliary:
  model_recommendation:
    provider: auto
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30
```

To enable it, set both a concrete configured provider and a model under that profile. `provider: auto` is intentionally disabled for this operation: the router must not implicitly choose a provider. The recommendation router is not a general auxiliary picker slot.

The backend makes one direct structured provider request for each manual recommendation request. It uses strict JSON-schema output validation and has no retry or fallback ladder. Draft text, router messages, credentials, and recommendation results are not persisted or logged.
