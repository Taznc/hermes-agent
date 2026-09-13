import { describe, expect, it } from 'vitest'

import { priorityLevel } from './priority'

describe('priorityLevel', () => {
  it('maps the scheduler values to the support-facing severity labels', () => {
    expect(priorityLevel(2)).toMatchObject({ id: 'critical', value: 2 })
    expect(priorityLevel(1)).toMatchObject({ id: 'high', value: 1 })
    expect(priorityLevel(0)).toMatchObject({ id: 'normal', value: 0 })
    expect(priorityLevel(-1)).toMatchObject({ id: 'low', value: -1 })
  })

  it('preserves an out-of-policy scheduler value as custom instead of silently remapping it', () => {
    expect(priorityLevel(7)).toEqual({ id: 'custom', value: 7 })
  })
})
