import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { PanelHeader, PanelRowMenu } from './panel'

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.setPointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

describe('PanelRowMenu', () => {
  afterEach(() => {
    cleanup()
  })

  it('opens its actions menu from the kebab without a tooltip', async () => {
    const onSelect = vi.fn()

    render(<PanelRowMenu items={[{ label: 'Rename', onSelect }]} />)

    const trigger = screen.getByRole('button', { name: 'Actions' })

    expect(trigger.closest('[data-slot="tooltip-trigger"]')).toBeNull()

    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Rename' }))

    expect(onSelect).toHaveBeenCalledOnce()
  })
})

describe('PanelHeader', () => {
  afterEach(() => {
    cleanup()
  })

  it('reserves clearance for the overlay close X by default when actions are present', () => {
    render(<PanelHeader actions={<button type="button">Do a thing</button>} title="Agents" />)
    const header = screen.getByRole('button', { name: 'Do a thing' }).closest('header')!
    expect(header.getAttribute('data-actions-clearance')).toBe('true')
  })

  it('drops the close-X clearance when the embedded presentation opts out', () => {
    render(
      <PanelHeader actions={<button type="button">Do a thing</button>} reserveActionsClearance={false} title="Agents" />
    )
    const header = screen.getByRole('button', { name: 'Do a thing' }).closest('header')!
    expect(header.getAttribute('data-actions-clearance')).toBe('false')
  })
})
