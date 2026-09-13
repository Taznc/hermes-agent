import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { validateCustomEndpoint, validateProviderCredential } from './api/config'
import { testMessagingPlatform } from './api/messaging'
import { getRecommendedDefaultModel, getUsageAnalytics } from './api/models'
import { getLatestSessionMessages, getSessionMessages } from './api/sessions'
import { getStarmapGraph } from './api/skills'
import { checkHermesUpdate, getElevenLabsVoices } from './api/system'
import { getToolsetModels } from './api/toolsets'
import { getStatus, STARTUP_REQUEST_TIMEOUT_MS } from './hermes'

// Table-driven check for the "slow-by-nature" call budgets carved out of the
// web-shim's default 30s timeout (kanban t_2a78aead). Each of these hits a
// live network probe, model listing, transcript read, or boot-burst read
// that regularly outruns the generic default — they get an explicit,
// larger `timeoutMs` so a slow-but-alive backend isn't mistaken for a dead
// one. Anything NOT in this table should fall through to the shim's 30s
// default (verified by the getStatus liveness-poll case below).
const SESSION_LIST_REQUEST_TIMEOUT_MS = 60_000

describe('explicit slow-call timeout budgets', () => {
  let api: ReturnType<typeof vi.fn>

  beforeEach(() => {
    api = vi.fn().mockResolvedValue({ messages: [], models: [] })
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api }
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  const cases: Array<{ name: string; expectedTimeoutMs: number; call: () => Promise<unknown> }> = [
    {
      name: 'validateProviderCredential (config.ts) — live provider probe',
      expectedTimeoutMs: 60_000,
      call: () => validateProviderCredential('provider.anthropic.api_key', 'sk-test')
    },
    {
      name: 'validateCustomEndpoint (config.ts) — live endpoint probe',
      expectedTimeoutMs: 60_000,
      call: () => validateCustomEndpoint({ base_url: 'https://example.test', model: 'test-model', name: 'ep' })
    },
    {
      name: 'testMessagingPlatform (messaging.ts) — live platform probe',
      expectedTimeoutMs: 60_000,
      call: () => testMessagingPlatform('telegram')
    },
    {
      name: 'getToolsetModels (toolsets.ts) — live model listing',
      expectedTimeoutMs: 60_000,
      call: () => getToolsetModels('search')
    },
    {
      name: 'checkHermesUpdate (system.ts) — network update check',
      expectedTimeoutMs: 60_000,
      call: () => checkHermesUpdate()
    },
    {
      name: 'getElevenLabsVoices (system.ts) — live voice listing',
      expectedTimeoutMs: 60_000,
      call: () => getElevenLabsVoices()
    },
    {
      name: 'getStarmapGraph (skills.ts) — server-side graph build',
      expectedTimeoutMs: 60_000,
      call: () => getStarmapGraph()
    },
    {
      name: 'getSessionMessages (sessions.ts) — transcript read',
      expectedTimeoutMs: SESSION_LIST_REQUEST_TIMEOUT_MS,
      call: () => getSessionMessages('sess-1')
    },
    {
      name: 'getLatestSessionMessages (sessions.ts) — transcript read',
      expectedTimeoutMs: SESSION_LIST_REQUEST_TIMEOUT_MS,
      call: () => getLatestSessionMessages('sess-1')
    },
    {
      name: 'getUsageAnalytics (models.ts) — boot-burst read',
      expectedTimeoutMs: STARTUP_REQUEST_TIMEOUT_MS,
      call: () => getUsageAnalytics()
    },
    {
      name: 'getRecommendedDefaultModel (models.ts) — boot-burst read',
      expectedTimeoutMs: STARTUP_REQUEST_TIMEOUT_MS,
      call: () => getRecommendedDefaultModel('anthropic')
    }
  ]

  it.each(cases)('$name carries timeoutMs=$expectedTimeoutMs', async ({ call, expectedTimeoutMs }) => {
    await call()

    expect(api).toHaveBeenCalledWith(expect.objectContaining({ timeoutMs: expectedTimeoutMs }))
  })

  it('getStatus (the intentional liveness poll) carries NO explicit timeoutMs', async () => {
    await getStatus()

    const [request] = api.mock.calls[0] as [Record<string, unknown>]

    expect(request).not.toHaveProperty('timeoutMs')
  })
})
