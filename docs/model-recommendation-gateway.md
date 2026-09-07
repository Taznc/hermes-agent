# Desktop model recommendation gateway contract

`model_recommendation.get` is a profile-scoped JSON-RPC read operation for the Desktop composer. It is advisory only: it does not create or alter a session, model selection, reasoning effort, or provider configuration.

## Request

```json
{
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

## Configuration

The profile-scoped default is inert:

```yaml
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
