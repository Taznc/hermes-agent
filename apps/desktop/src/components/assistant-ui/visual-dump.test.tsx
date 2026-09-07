import { render } from '@testing-library/react'
import { writeFileSync } from 'node:fs'
import { describe, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { setClarifyRequest } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { ClarifyTool } from './clarify-tool'

vi.mock('@assistant-ui/react', () => ({ useAuiState: () => true }))

const props = (args: unknown) =>
  ({
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result: undefined,
    resume: vi.fn(),
    status: { type: 'running' },
    toolCallId: 'shot',
    toolName: 'clarify',
    type: 'tool-call'
  }) as never

describe('visual dump', () => {
  it('single-question card', () => {
    $activeSessionId.set('s1')
    $gateway.set({ request: vi.fn() } as never)
    setClarifyRequest({
      choices: ['Fix it in the fork (Recommended)', 'Fork into our own plugin', 'Do both'],
      multiSelect: false,
      question: 'Own tool vs modify Hermes clarify?',
      requestId: 'r1',
      sessionId: 's1'
    })
    const { container } = render(
      <I18nProvider configClient={null} initialLocale="en">
        <ClarifyTool {...props({ question: 'Own tool vs modify Hermes clarify?' })} />
      </I18nProvider>
    )
    writeFileSync('/tmp/shot-single.html', container.innerHTML)
  })
})
