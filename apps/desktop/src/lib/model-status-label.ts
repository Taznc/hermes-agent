import { DEFAULT_REASONING_EFFORT, reasoningEffortLabel } from '@/lib/reasoning-effort'

/** Which model/provider pair a picker should mark "current". SessionView state
 *  also drives the composer label, so a complete pair there wins over an older
 *  `model.options` response. During initial hydration (or pre-session startup),
 *  options remain the fallback. Pick one complete pair before mixing fields so
 *  a model is never shown under a different provider. */
export function currentPickerSelection(
  store: { model: string; provider: string },
  options?: { model?: string; provider?: string }
): { model: string; provider: string } {
  const storeSelection = {
    model: String(store.model || ''),
    provider: String(store.provider || '')
  }

  const optionsSelection = {
    model: String(options?.model || ''),
    provider: String(options?.provider || '')
  }

  if (storeSelection.model && storeSelection.provider) {
    return storeSelection
  }

  if (optionsSelection.model && optionsSelection.provider) {
    return optionsSelection
  }

  return {
    model: storeSelection.model || optionsSelection.model,
    provider: storeSelection.provider || optionsSelection.provider
  }
}

/** Strip provider prefix and normalize for display. */
export function modelBaseId(model: string): string {
  const trimmed = model.trim()
  const slash = trimmed.lastIndexOf('/')

  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}

// Trailing model-id variants that should render as a grayed tag beside the
// name (e.g. "Opus 4.8" + "Fast") rather than collapsing two distinct ids to
// the same display name.
const VARIANT_TAGS: ReadonlyArray<readonly [RegExp, string]> = [
  [/-fast$/i, 'Fast'],
  [/-thinking$/i, 'Thinking'],
  [/-preview$/i, 'Preview'],
  [/-latest$/i, 'Latest']
]

const titleCase = (text: string): string => text.replace(/\b\w/g, char => char.toUpperCase()).trim()

function prettifyBase(base: string): string {
  if (/^claude-/i.test(base)) {
    return titleCase(base.replace(/^claude-/i, '').replace(/-/g, ' '))
  }

  if (/^gpt-/i.test(base)) {
    return base.replace(/^gpt-/i, 'GPT-')
  }

  if (/^gemini-/i.test(base)) {
    return base.replace(/^gemini-/i, 'Gemini ').replace(/-/g, ' ')
  }

  return titleCase(base.replace(/-/g, ' '))
}

/** Split a model id into a clean display name plus an optional grayed variant
 *  tag, so distinct ids (e.g. `…-4.8` vs `…-4.8-fast`) don't collapse. */
export function modelDisplayParts(model: string): { name: string; tag: string } {
  let base = modelBaseId(model)
  let tag = ''

  for (const [pattern, label] of VARIANT_TAGS) {
    if (pattern.test(base)) {
      tag = label
      base = base.replace(pattern, '')

      break
    }
  }

  // Drop a trailing date-pin (`…-20251101`) — snapshot noise, not a name.
  base = base.replace(/-\d{8}$/, '')

  return { name: prettifyBase(base) || model.trim() || 'No model', tag }
}

/** Friendly one-line model name for menus and the status bar. */
export function displayModelName(model: string): string {
  return modelDisplayParts(model).name
}

// Provider ids whose FAMILY reads differently than their raw slug — mirrors
// the small subset of `hermes_cli.models.CANONICAL_PROVIDERS` labels a
// session-sidebar chip actually needs to disambiguate (Phase 2.13). This is
// deliberately NOT a full copy of the backend's ~40-entry provider table:
// unlisted providers fall back to a title-cased slug, which is an accurate,
// non-misleading label (never implies a false Claude/Codex identity) even
// though it won't win a beauty contest for every long-tail provider id.
const PROVIDER_FAMILY_OVERRIDES: Readonly<Record<string, string>> = {
  anthropic: 'Claude',
  'claude-code': 'Claude',
  'openai-codex': 'Codex',
  'openai-api': 'OpenAI',
  custom: 'Custom'
}

/** Bare billing buckets that are not a routable provider identity on their
 *  own — mirrors `hermes_state._BARE_BILLING_PROVIDERS`. A session whose
 *  configured/served provider resolved to one of these has no opinion worth
 *  showing (never a Claude/Codex family). */
const BARE_PROVIDER_BUCKETS = new Set(['auto'])

/** Resolve a raw Hermes provider id (`session.configured_provider` /
 *  `session.served_provider`) to a short display family — "Claude",
 *  "Codex", or a title-cased fallback for every other provider. Returns
 *  `null` for empty/bare-bucket input so callers (legacy sessions, sessions
 *  that never resolved a provider) can render nothing instead of a
 *  misleading guess. */
export function providerFamilyLabel(provider: null | string | undefined): string | null {
  const raw = (provider || '').trim().toLowerCase()

  if (!raw || BARE_PROVIDER_BUCKETS.has(raw)) return null
  if (PROVIDER_FAMILY_OVERRIDES[raw]) return PROVIDER_FAMILY_OVERRIDES[raw]

  return raw
    .split(/[-_]+/)
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

/** Status bar trigger label — model name plus the live session state (effort/fast).
 *  `defaultEffort` is the profile's configured level, used when the surface has
 *  no explicit effort so the label never advertises a default the agent won't use. */
export function formatModelStatusLabel(
  model: string,
  options?: { defaultEffort?: string; fastMode?: boolean; reasoningEffort?: string }
): string {
  const name = displayModelName(model)

  if (!model.trim()) {
    return name
  }

  const parts: string[] = []

  // Fast is shown when the speed=fast param is on (options.fastMode) OR the
  // active model is a `…-fast` variant (fast via a separate model id).
  if (options?.fastMode || /-fast$/i.test(modelBaseId(model))) {
    parts.push('Fast')
  }

  // Always surface the effort so the current reasoning level is visible at a
  // glance, not just when non-default.
  parts.push(reasoningEffortLabel(options?.reasoningEffort || options?.defaultEffort || DEFAULT_REASONING_EFFORT))

  return `${name} · ${parts.join(' ')}`
}
