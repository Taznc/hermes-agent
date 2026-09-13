import { describe, expect, it } from 'vitest'

import { KANBAN_LOCALES } from './i18n'

const actionKeys = [
  'hermesActionsSection',
  'hermesActionExplain',
  'hermesActionFailure',
  'hermesActionReview',
  'hermesActionRough',
  'hermesActionScope',
  'hermesActionUnblock',
  'hermesActionsDetachedHint',
  'hermesActionsBoardUnavailable'
] as const

describe('Kanban Hermes action locales', () => {
  it.each(Object.entries(KANBAN_LOCALES))('%s supplies every seeded-action message', (_, locale) => {
    for (const key of actionKeys) {
      const message = locale[key]

      expect(typeof message).toBe('string')

      if (typeof message === 'string') {
        expect(message.trim()).not.toBe('')
      }
    }
  })
})
