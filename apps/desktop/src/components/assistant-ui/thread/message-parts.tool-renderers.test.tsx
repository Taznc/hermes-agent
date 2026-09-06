/**
 * The `toolRenderers` plugin seam (t_305e8c3b): a plugin claims a tool name
 * and owns its transcript card; core stays the untouched default.
 *
 * `Fallback` below IS `MESSAGE_PARTS_COMPONENTS.tools.Fallback` — the exact
 * component assistant-ui mounts for every tool-call part — so these tests
 * exercise the real wiring, not a stand-in.
 */
import { cleanup, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import { TOOL_RENDERERS_AREA, type ToolRendererContribution } from '@/lib/tool-renderers'

vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-1', status: { type: 'complete' } }, thread: { isRunning: false } })
}))

const { MESSAGE_PARTS_COMPONENTS } = await import('./message-parts')

const Fallback = MESSAGE_PARTS_COMPONENTS.tools.Fallback

type FallbackProps = ComponentProps<typeof Fallback>

/** A tool name the core chain has no special case for — every registration
 *  test uses this so a match can only come from the plugin registry, and any
 *  fallthrough exercises core's real generic `ToolFallback` row (proof the
 *  boundary actually degrades to a full render, not a stub). */
const PLAIN_TOOL = 'demo_probe_tool'

function baseProps(overrides: Partial<FallbackProps> = {}): FallbackProps {
  return {
    args: { path: '/tmp/demo.txt' },
    result: { ok: true },
    toolCallId: 'call-1',
    toolName: PLAIN_TOOL,
    ...overrides
  } as unknown as FallbackProps
}

function contributeRenderer(id: string, contribution: ToolRendererContribution) {
  return registry.register({ area: TOOL_RENDERERS_AREA, data: contribution, id })
}

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

describe('toolRenderers: registration + override', () => {
  it('renders the claiming plugin card instead of the core row', () => {
    disposers.push(
      contributeRenderer('demo:probe', {
        toolName: PLAIN_TOOL,
        render: () => <span data-testid="plugin-card">mine</span>
      })
    )

    const { container } = render(<Fallback {...baseProps()} />)

    expect(screen.getByTestId('plugin-card')).toBeTruthy()
    // The core generic row's own marker must NOT also be present — the
    // plugin card replaced it, it didn't sit beside it.
    expect(container.querySelector('[data-slot="tool-block"]')).toBeNull()
  })

  it('never renders the plugin card for a DIFFERENT tool name', () => {
    disposers.push(
      contributeRenderer('demo:probe', {
        toolName: 'some_other_tool',
        render: () => <span data-testid="plugin-card">mine</span>
      })
    )

    render(<Fallback {...baseProps()} />)

    expect(screen.queryByTestId('plugin-card')).toBeNull()
  })
})

describe('toolRenderers: duplicate toolName precedence', () => {
  it('is asserted as last-registration-wins, not incidental iteration order', () => {
    disposers.push(contributeRenderer('demo:a', { toolName: PLAIN_TOOL, render: () => <span>first</span> }))
    disposers.push(contributeRenderer('demo:b', { toolName: PLAIN_TOOL, render: () => <span>second</span> }))

    render(<Fallback {...baseProps()} />)

    expect(screen.getByText('second')).toBeTruthy()
    expect(screen.queryByText('first')).toBeNull()
  })

  it('still resolves to the survivor after the loser is disposed', () => {
    const disposeA = contributeRenderer('demo:a', { toolName: PLAIN_TOOL, render: () => <span>first</span> })
    disposers.push(contributeRenderer('demo:b', { toolName: PLAIN_TOOL, render: () => <span>second</span> }))
    disposeA()

    render(<Fallback {...baseProps()} />)

    expect(screen.getByText('second')).toBeTruthy()
  })
})

describe('toolRenderers: a throwing plugin renderer degrades to the core row', () => {
  it('does not blank the transcript — the core ToolFallback row still paints', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    disposers.push(
      contributeRenderer('demo:boom', {
        toolName: PLAIN_TOOL,
        render: () => {
          throw new Error('plugin renderer exploded')
        }
      })
    )

    let container: HTMLElement | undefined

    expect(() => {
      ;({ container } = render(<Fallback {...baseProps()} />))
    }).not.toThrow()

    // Fell all the way back to core's generic tool row, not a dead slot.
    expect(container?.querySelector('[data-slot="tool-block"]')).not.toBeNull()

    consoleError.mockRestore()
  })
})

describe('toolRenderers: no plugin registered — the core chain is untouched', () => {
  it('still hides a `todo` tool the exact way core always has', () => {
    const { container } = render(<Fallback {...baseProps({ toolName: 'todo' })} />)

    expect(container.textContent).toBe('')
  })

  it('still hides a non-error `react_to_message` tool the exact way core always has', () => {
    const { container } = render(<Fallback {...baseProps({ toolName: 'react_to_message', isError: false })} />)

    expect(container.textContent).toBe('')
  })

  it('still falls through an unclaimed tool name to the core generic row', () => {
    const { container } = render(<Fallback {...baseProps()} />)

    expect(container.querySelector('[data-slot="tool-block"]')).not.toBeNull()
  })
})
