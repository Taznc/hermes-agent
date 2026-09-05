import { act, renderHook } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { useOnProfileSwitch } from './use-on-profile-switch'

afterEach(() => {
  $activeGatewayProfile.set('default')
})

describe('useOnProfileSwitch', () => {
  // Regression: the old one-shot `first` ref treated Strict Mode's mandatory
  // mount → cleanup → re-mount effect replay as a real profile switch, firing
  // onSwitch on every mount and wiping local drafts/state before the user
  // ever touched the profile selector (upstream #74824).
  it('does not fire on mount, including under StrictMode double-invoke', () => {
    const onSwitch = vi.fn()

    renderHook(() => useOnProfileSwitch(onSwitch), {
      wrapper: StrictMode
    })

    expect(onSwitch).not.toHaveBeenCalled()
  })

  it('fires when the active gateway profile actually changes', () => {
    const onSwitch = vi.fn()

    renderHook(() => useOnProfileSwitch(onSwitch), {
      wrapper: StrictMode
    })

    act(() => {
      $activeGatewayProfile.set('coder')
    })

    expect(onSwitch).toHaveBeenCalledTimes(1)
  })

  it('does not fire when the profile atom is set to the same value', () => {
    const onSwitch = vi.fn()

    renderHook(() => useOnProfileSwitch(onSwitch), {
      wrapper: StrictMode
    })

    act(() => {
      $activeGatewayProfile.set('default')
    })

    expect(onSwitch).not.toHaveBeenCalled()
  })

  it('does not fire when the raw value changes but the normalized key does not', () => {
    const onSwitch = vi.fn()

    renderHook(() => useOnProfileSwitch(onSwitch), {
      wrapper: StrictMode
    })

    // '' and ' default ' both normalize to 'default' — not a real switch.
    act(() => {
      $activeGatewayProfile.set('')
    })
    act(() => {
      $activeGatewayProfile.set(' default ')
    })

    expect(onSwitch).not.toHaveBeenCalled()
  })

  it('fires exactly once per real switch even across repeated StrictMode remounts', () => {
    const onSwitch = vi.fn()

    const { unmount, rerender } = renderHook(() => useOnProfileSwitch(onSwitch), {
      wrapper: StrictMode
    })

    // Simulate StrictMode-style remounts that don't correspond to a real
    // switch (e.g. a parent re-render): the hook must stay quiet.
    rerender()
    rerender()
    expect(onSwitch).not.toHaveBeenCalled()

    act(() => {
      $activeGatewayProfile.set('research')
    })
    expect(onSwitch).toHaveBeenCalledTimes(1)

    unmount()
  })
})
