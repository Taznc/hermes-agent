import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Regression test for the bug where a stalled (not rejected) config/schema
// fetch left the Settings panel on a bare skeleton forever, with no error and
// no retry affordance. Root cause: getHermesConfigSchema() (and several
// sibling boot-burst calls) omitted `timeoutMs`, so react-query's `isError`
// never flipped and config-settings.tsx's retry-UI branch never activated.
// See apps/desktop/src/api/config.ts and src/hermes.test.ts's
// "gives the whole startup data burst the long timeout" test for the API
// contract half of this fix.

const getHermesConfigRecord = vi.fn()
const getHermesConfigSchema = vi.fn()
const getElevenLabsVoices = vi.fn()
const saveHermesConfig = vi.fn()

vi.mock('@/hermes', () => ({
  getHermesConfigRecord: () => getHermesConfigRecord(),
  getHermesConfigSchema: () => getHermesConfigSchema(),
  getElevenLabsVoices: () => getElevenLabsVoices(),
  saveHermesConfig: (config: unknown) => saveHermesConfig(config),
  profileScopeKey: (profile?: null | string) => profile ?? 'default'
}))

vi.mock('@/store/profile', async () => {
  const { atom } = await import('nanostores')

  return {
    $activeGatewayProfile: atom('default'),
    $profiles: atom([]),
    normalizeProfileKey: (name?: null | string) => (name ?? '').trim() || 'default',
    refreshProfiles: vi.fn(async () => [])
  }
})

vi.mock('@/store/settings-scope', async () => {
  const { atom, computed } = await import('nanostores')
  const override = atom<null | string>(null)

  return {
    $settingsScopeOverride: override,
    $settingsRequestProfile: computed(override, (o): string | undefined => o ?? undefined)
  }
})

vi.mock('@/store/keep-awake', async () => {
  const { atom } = await import('nanostores')

  return { $keepAwake: atom(false), setKeepAwake: vi.fn() }
})

vi.mock('@/store/disable-f12', async () => {
  const { atom } = await import('nanostores')

  return { $disableF12: atom(false), setDisableF12: vi.fn() }
})

vi.mock('@/store/data-url-read-max', async () => {
  const { atom } = await import('nanostores')

  return {
    $dataUrlReadMaxMb: atom(10),
    DATA_URL_READ_DEFAULT_MAX_MB: 10,
    DATA_URL_READ_MAX_MAX_MB: 100,
    DATA_URL_READ_MIN_MAX_MB: 1,
    clampDataUrlReadMaxMb: (value: unknown) => Number(value) || 10,
    refreshDataUrlReadMaxMb: vi.fn(async () => 10),
    setDataUrlReadMaxMb: vi.fn(async (value: number) => value)
  }
})

vi.mock('@/store/confirm', () => ({ confirm: vi.fn(async () => true) }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/store/projects', () => ({
  repoDiscoveryPolicyFromConfig: vi.fn(() => ({})),
  repoDiscoveryPolicySignature: vi.fn(() => ''),
  scanAndRecordRepos: vi.fn(async () => undefined)
}))

function renderConfigSettings(activeSectionId = 'workspace') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const importInputRef = { current: null }

  return import('./config-settings').then(({ ConfigSettings }) =>
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ConfigSettings activeSectionId={activeSectionId} importInputRef={importInputRef} />
        </QueryClientProvider>
      </MemoryRouter>
    )
  )
}

beforeEach(() => {
  getElevenLabsVoices.mockResolvedValue({ available: false, voices: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ConfigSettings — schema fetch stalls or times out', () => {
  it('shows the retry-capable empty state once the schema fetch rejects, instead of an indefinite skeleton', async () => {
    // getHermesConfigRecord resolves normally (config loads fine); the schema
    // fetch is the one that stalls, mirroring the exact bug reproduction (a
    // hung/timed-out /api/config/schema while /api/config succeeds).
    getHermesConfigRecord.mockResolvedValue({})

    let rejectSchema: (err: Error) => void = () => undefined
    getHermesConfigSchema.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectSchema = reject
      })
    )

    await renderConfigSettings()

    // Bare skeleton while both requests are in flight — the pre-fix state,
    // which is correct UNTIL the fetch resolves or rejects.
    expect(document.querySelector('[data-slot="skeleton"]')).toBeTruthy()
    expect(screen.queryByText('Settings failed to load')).toBeNull()

    // Simulate the fetch layer's timeout rejection (what a real timeoutMs +
    // AbortController produces once the fix in api/config.ts is applied —
    // without it, this promise would never settle and the panel would stay
    // on the skeleton forever with no way to recover).
    rejectSchema(new Error('Request timed out after 60000ms'))

    // The retry-capable error state must appear — not an indefinite skeleton.
    expect(await screen.findByText('Settings failed to load')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Refresh skills' })).toBeTruthy()
  })

  it('recovers via the retry button once schema is refetched successfully', async () => {
    getHermesConfigRecord.mockResolvedValue({})
    getHermesConfigSchema.mockRejectedValueOnce(new Error('Request timed out after 60000ms'))

    await renderConfigSettings()

    const retry = await screen.findByRole('button', { name: 'Refresh skills' })

    getHermesConfigSchema.mockResolvedValueOnce({ fields: {}, category_order: [] })
    retry.click()

    // Retry-capable error clears once the schema fetch actually succeeds.
    await vi.waitFor(() => expect(screen.queryByText('Settings failed to load')).toBeNull())
  })
})
