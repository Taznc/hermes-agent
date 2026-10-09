import { describe, expect, it } from 'vitest'

import { ASK_SIDE_MARKER, isAskSideQuestion } from './side-question'

describe('isAskSideQuestion', () => {
  it('recognises a side question sent by the Ask card', () => {
    expect(isAskSideQuestion(`${ASK_SIDE_MARKER} [The user has an open question card…]`)).toBe(true)
    expect(isAskSideQuestion(`  ${ASK_SIDE_MARKER} x`)).toBe(true)
  })

  it('leaves an ordinary /btw question alone', () => {
    expect(isAskSideQuestion('which file was that error in?')).toBe(false)
    expect(isAskSideQuestion(`about ${ASK_SIDE_MARKER}`)).toBe(false)
    expect(isAskSideQuestion(undefined)).toBe(false)
    expect(isAskSideQuestion(42)).toBe(false)
  })
})
