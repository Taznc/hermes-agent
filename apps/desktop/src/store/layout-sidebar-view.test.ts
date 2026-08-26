import { beforeEach, describe, expect, it } from 'vitest'

import {
  $sidebarGrouping,
  $sidebarListLimit,
  $sidebarOrdering,
  $sidebarRowMeta,
  $sidebarViewCustomized,
  resetSidebarView,
  setSidebarGrouping,
  setSidebarListLimit,
  setSidebarOrdering,
  SIDEBAR_LIST_LIMIT_OPTIONS,
  toggleSidebarRowMeta,
  toggleSidebarStatusFilter
} from './layout'
import { $showAllProfiles } from './profile'

beforeEach(() => {
  $showAllProfiles.set(false)
  resetSidebarView()
})

describe('the sidebar as it ships', () => {
  it('groups by date, sorts by recency, and pins the timestamp and preview', () => {
    expect($sidebarGrouping.get()).toBe('date')
    expect($sidebarOrdering.get()).toBe('updated')
    expect($sidebarRowMeta.get()).toEqual(['preview', 'updated'])
  })

  it('offers no reset until something actually moves off the defaults', () => {
    expect($sidebarViewCustomized.get()).toBe(false)

    toggleSidebarRowMeta('tokens')

    expect($sidebarViewCustomized.get()).toBe(true)
  })

  it('is what reset puts back — every knob, not just the filters', () => {
    setSidebarGrouping('project')
    setSidebarOrdering('cost')
    toggleSidebarRowMeta('updated')
    toggleSidebarRowMeta('cost')
    toggleSidebarStatusFilter('working')

    resetSidebarView()

    expect($sidebarGrouping.get()).toBe('date')
    expect($sidebarOrdering.get()).toBe('updated')
    expect($sidebarRowMeta.get()).toEqual(['preview', 'updated'])
    expect($sidebarViewCustomized.get()).toBe(false)
  })

  it('ships by date in the all-profiles scope too, and resets back to it', () => {
    $showAllProfiles.set(true)
    setSidebarGrouping('profile')

    resetSidebarView()

    expect($sidebarGrouping.get()).toBe('date')
    expect($sidebarViewCustomized.get()).toBe(false)
  })

  it('resets the scope the user is not looking at, so flipping the rail cannot restore it', () => {
    setSidebarGrouping('status')
    $showAllProfiles.set(true)
    setSidebarGrouping('profile')

    resetSidebarView()
    $showAllProfiles.set(false)

    expect($sidebarGrouping.get()).toBe('date')
  })

  it('turns all-profiles on when the user groups by profile, since that is the ask', () => {
    setSidebarGrouping('profile')

    expect($showAllProfiles.get()).toBe(true)
    expect($sidebarGrouping.get()).toBe('profile')
  })
})

describe('the sidebar list-length setting', () => {
  it('ships as "all" — every unarchived row, no load-more affordance', () => {
    expect($sidebarListLimit.get()).toBe('all')
  })

  it('offers the documented picks in order: all, 10, 25, 50, 100', () => {
    expect(SIDEBAR_LIST_LIMIT_OPTIONS).toEqual(['all', 10, 25, 50, 100])
  })

  it('trims to the numeric pick and reports the view as customized', () => {
    setSidebarListLimit(25)

    expect($sidebarListLimit.get()).toBe(25)
    expect($sidebarViewCustomized.get()).toBe(true)
  })

  it('is not "customized" while still on the shipped default', () => {
    expect($sidebarViewCustomized.get()).toBe(false)

    setSidebarListLimit('all')

    expect($sidebarViewCustomized.get()).toBe(false)
  })

  it('resetSidebarView returns it to "all" alongside every other knob', () => {
    setSidebarGrouping('project')
    setSidebarListLimit(50)

    resetSidebarView()

    expect($sidebarListLimit.get()).toBe('all')
    expect($sidebarViewCustomized.get()).toBe(false)
  })
})
