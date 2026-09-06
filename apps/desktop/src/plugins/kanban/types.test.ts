import { describe, expect, it } from 'vitest'
import { SEVERITY_TONE, type Diagnostic } from './types'

describe('SEVERITY_TONE', () => {
  it('has a real, non-undefined tone for every Diagnostic severity, including info', () => {
    const severities: Diagnostic['severity'][] = ['critical', 'error', 'warning', 'info']
    for (const sev of severities) {
      const tone = SEVERITY_TONE[sev]
      expect(tone).toBeTypeOf('string')
      expect(tone.length).toBeGreaterThan(0)
    }
  })

  it('gives info a distinct, non-destructive tone from error/critical', () => {
    expect(SEVERITY_TONE.info).not.toBe(SEVERITY_TONE.error)
    expect(SEVERITY_TONE.info).not.toBe(SEVERITY_TONE.critical)
  })
})
