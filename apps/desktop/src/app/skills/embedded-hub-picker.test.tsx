// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $paneStates } from '@/store/panes'

import { EmbeddedHubPicker } from './embedded-hub-picker'

// Keep the hub-action pipeline out of the way: this file only exercises the
// top-edge sash drag. The map shape matches what useStoreSelector reads.
vi.mock('@/store/hub-actions', async () => {
  const { map } = await import('nanostores')

  return {
    $hubActions: map({}),
    installHubSkill: vi.fn(),
    UPDATE_ALL_KEY: '__update_all__',
    updateHubSkills: vi.fn()
  }
})

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const HUB_PANE_ID = 'capabilities-hub'

const dispatch = (event: Event) => act(() => void window.dispatchEvent(event))

const renderSash = () => {
  const { container } = render(<EmbeddedHubPicker installedNames={new Set()} />)
  const sash = container.querySelector('.cursor-row-resize')

  if (!(sash instanceof HTMLElement)) {
    throw new Error('hub sash not rendered')
  }

  return sash
}

beforeEach(() => {
  $paneStates.set({})
})

afterEach(() => {
  cleanup()
  $paneStates.set({})
})

describe('EmbeddedHubPicker sash', () => {
  it('resizes on pointermove while dragging (harness sanity)', () => {
    const sash = renderSash()

    fireEvent.pointerDown(sash, { button: 0, clientY: 500 })
    dispatch(new PointerEvent('pointermove', { clientY: 400 }))

    expect($paneStates.get()[HUB_PANE_ID]?.heightOverride).toBeTypeOf('number')
  })

  it('tears the drag down on pointercancel, not just pointerup', () => {
    const sash = renderSash()

    fireEvent.pointerDown(sash, { button: 0, clientY: 500 })
    // OS/browser cancelled the stream (window drag-out, touch cancel, system
    // gesture): a stray pointermove must not keep resizing the hub.
    dispatch(new Event('pointercancel'))
    dispatch(new PointerEvent('pointermove', { clientY: 400 }))

    expect($paneStates.get()[HUB_PANE_ID]?.heightOverride).toBeUndefined()
  })
})
