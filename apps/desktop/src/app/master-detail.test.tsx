// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $paneStates } from '@/store/panes'

import { DetailPane, MasterDetail } from './master-detail'

// Sash drags end through more paths than pointerup: the OS/browser cancels the
// pointer stream on window drag-out, touch cancel, or a system gesture. A
// cancelled stream must tear the drag down exactly like pointerup — otherwise
// the pointermove listener stays live and the pane keeps resizing with no
// button held (DESIGN.md: cancellation is synchronous). Same contract as
// hud/resize-handle.ts and hud/composer-drag.ts.

const dispatch = (event: Event) => act(() => void window.dispatchEvent(event))

beforeEach(() => {
  $paneStates.set({})
})

afterEach(() => {
  cleanup()
  $paneStates.set({})
})

describe('MasterDetail split sash', () => {
  const renderSplit = () => {
    const { container } = render(
      <MasterDetail resizeId="test-split" split="wide">
        <div>list</div>
        <div>detail</div>
      </MasterDetail>
    )

    const sash = container.querySelector('.cursor-col-resize')

    if (!(sash instanceof HTMLElement)) {
      throw new Error('split sash not rendered')
    }

    return sash
  }

  it('resizes on pointermove while dragging (harness sanity)', () => {
    const sash = renderSplit()

    fireEvent.pointerDown(sash, { button: 0, clientX: 300 })
    dispatch(new PointerEvent('pointermove', { clientX: 340 }))

    expect($paneStates.get()['test-split']?.widthOverride).toBeTypeOf('number')
  })

  it('stops resizing after pointerup', () => {
    const sash = renderSplit()

    fireEvent.pointerDown(sash, { button: 0, clientX: 300 })
    dispatch(new PointerEvent('pointerup', {}))
    dispatch(new PointerEvent('pointermove', { clientX: 340 }))

    expect($paneStates.get()['test-split']?.widthOverride).toBeUndefined()
  })

  it('tears the drag down on pointercancel, not just pointerup', () => {
    const sash = renderSplit()

    fireEvent.pointerDown(sash, { button: 0, clientX: 300 })
    dispatch(new Event('pointercancel'))
    // The stream is dead: a stray pointermove must not keep resizing.
    dispatch(new PointerEvent('pointermove', { clientX: 340 }))

    expect($paneStates.get()['test-split']?.widthOverride).toBeUndefined()
  })
})

describe('DetailPane sash', () => {
  const renderPane = () => {
    const { container } = render(
      <DetailPane id="test-pane" title="pane">
        body
      </DetailPane>
    )

    const sash = container.querySelector('.cursor-row-resize')

    if (!(sash instanceof HTMLElement)) {
      throw new Error('pane sash not rendered')
    }

    return sash
  }

  it('resizes on pointermove while dragging (harness sanity)', () => {
    const sash = renderPane()

    fireEvent.pointerDown(sash, { button: 0, clientY: 500 })
    dispatch(new PointerEvent('pointermove', { clientY: 400 }))

    expect($paneStates.get()['test-pane']?.heightOverride).toBeTypeOf('number')
  })

  it('tears the drag down on pointercancel, not just pointerup', () => {
    const sash = renderPane()

    fireEvent.pointerDown(sash, { button: 0, clientY: 500 })
    dispatch(new Event('pointercancel'))
    dispatch(new PointerEvent('pointermove', { clientY: 400 }))

    expect($paneStates.get()['test-pane']?.heightOverride).toBeUndefined()
  })
})
