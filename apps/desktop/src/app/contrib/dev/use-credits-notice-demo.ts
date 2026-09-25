import { useEffect } from 'react'

import type * as CreditsNoticeDemo from './credits-notice-demo'

/** Keep the demo module behind the DEV-only dynamic import in production. */
export function useCreditsNoticeDemo(load?: () => Promise<typeof CreditsNoticeDemo>): void {
  useEffect(() => {
    if (!import.meta.env.DEV) {
      return
    }

    let active = true
    let dispose: (() => void) | undefined

    void (load ? load() : import('./credits-notice-demo')).then(m => {
      if (active) {
        dispose = m.installCreditsNoticeDemo()
      }
    })

    return () => {
      active = false
      dispose?.()
    }
  }, [load])
}
