import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { TerminalPaneChrome } from './chrome'
import { $activeTerminalId, $terminals } from './terminals'

const originalDesktop = window.hermesDesktop

function setDesktop(value: typeof window.hermesDesktop | undefined) {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value,
    writable: true
  })
}

afterEach(() => {
  cleanup()
  $terminals.set([])
  $activeTerminalId.set(null)
  setDesktop(originalDesktop)
})

describe('TerminalPaneChrome web capability', () => {
  it('shows an honest unavailable state instead of a blank terminal when the PTY bridge is absent', () => {
    setDesktop(undefined)

    const view = render(<TerminalPaneChrome />)

    expect(screen.getByText('Embedded terminal unavailable')).toBeTruthy()
    expect(screen.getByText('Interactive shell access requires the Hermes desktop app.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'New terminal' })).toBeNull()
    expect(view.container.querySelector('[data-terminal-slot]')).toBeNull()
  })

  it('keeps the interactive terminal surface unchanged when the PTY bridge is present', () => {
    setDesktop({ ...(originalDesktop ?? {}), terminal: {} } as typeof window.hermesDesktop)
    $terminals.set([{ auto: true, cwd: '/repo', id: 'term-1', kind: 'user', title: 'zsh' }])
    $activeTerminalId.set('term-1')

    const view = render(<TerminalPaneChrome />)

    expect(view.container.querySelector('[data-terminal-slot]')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeTruthy()
    expect(screen.queryByText('Embedded terminal unavailable')).toBeNull()
  })

  it('does not go blank when a persisted user tab remains active beside an agent mirror', () => {
    setDesktop(undefined)
    $terminals.set([
      { auto: true, cwd: '/repo', id: 'term-1', kind: 'user', title: 'zsh' },
      { auto: false, cwd: '', id: 'agent-1', kind: 'agent', procId: 'proc-1', title: 'tests' }
    ])
    $activeTerminalId.set('term-1')

    render(<TerminalPaneChrome />)

    expect(screen.getByText('Embedded terminal unavailable')).toBeTruthy()
    expect(screen.getByRole('tab', { name: '1. tests' })).toBeTruthy()
  })

  it('keeps read-only agent output tabs available without offering a new interactive terminal', () => {
    setDesktop(undefined)
    $terminals.set([{ auto: false, cwd: '', id: 'agent-1', kind: 'agent', procId: 'proc-1', title: 'tests' }])
    $activeTerminalId.set('agent-1')

    const view = render(<TerminalPaneChrome />)

    expect(view.container.querySelector('[data-terminal-slot]')).not.toBeNull()
    expect(screen.getByRole('tab', { name: '1. tests' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'New terminal' })).toBeNull()
    expect(screen.queryByText('Embedded terminal unavailable')).toBeNull()
  })
})
