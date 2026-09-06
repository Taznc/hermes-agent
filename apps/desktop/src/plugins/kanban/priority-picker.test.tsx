import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { PriorityPicker } from './priority-picker'

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

vi.mock('./ui', () => ({
  useKanban: () => ({
    priorityCritical: 'priorityCritical',
    priorityCustom: (value: number) => `priorityCustom:${value}`,
    priorityHigh: 'priorityHigh',
    priorityLow: 'priorityLow',
    priorityNormal: 'priorityNormal'
  })
}))

describe('PriorityPicker', () => {
  it('shows the support-facing severity and writes its scheduler value when selected', async () => {
    const onChange = vi.fn()
    render(<PriorityPicker onChange={onChange} priority={0} />)

    fireEvent.pointerDown(screen.getByRole('button', { name: 'priorityNormal' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByText('priorityCritical'))

    expect(onChange).toHaveBeenCalledWith(2)
  })
})
