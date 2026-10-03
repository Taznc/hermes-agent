/**
 * Fork backend capability, as a module store the overlay and the switcher
 * item both read.
 *
 * `$forkBackend` is `null` until the probe resolves, then true/false for the
 * active connection. Everything the fork adds is gated on it being `true`, so
 * a backend without the `fork-kanban` plugin renders upstream's board exactly:
 * no All Boards option, no trace affordance (counts stay upstream's own), and
 * no errors or toasts — the probe swallows every failure.
 */

import { useStore as useValue } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect } from 'react'

import { probeForkBackend } from '@/fork/kanban/all-boards'
import { $activeConnectionId } from '@/store/connections'

export const $forkBackend = atom<boolean | null>(null)

let probedFor: null | string = null
let inflight: null | Promise<boolean> = null

/** Probe once per connection (idempotent; concurrent callers share it). */
export function ensureForkBackend(): Promise<boolean> {
  const scope = $activeConnectionId.get() ?? 'local'

  if (probedFor === scope && inflight) {
    return inflight
  }

  probedFor = scope
  $forkBackend.set(null)
  inflight = probeForkBackend().then(ok => {
    if (probedFor === scope) {
      $forkBackend.set(ok)
    }

    return ok
  })

  return inflight
}

/** The capability for the active connection; re-probes on a connection switch. */
export function useForkBackend(): boolean | null {
  useEffect(() => $activeConnectionId.subscribe(() => void ensureForkBackend()), [])

  return useValue($forkBackend)
}

/** Test seam: forget the probe so the next mount probes again. */
export function resetForkBackend(): void {
  probedFor = null
  inflight = null
  $forkBackend.set(null)
}
