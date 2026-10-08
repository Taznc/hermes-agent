import { useMemo } from 'react'

import { type GatewayHealthPillInput, statusBarGatewayHealth } from '@/lib/gateway-health-pill'

/** Memoize only the unchanged upstream derivation. Callers construct input and
 * copy each render, so depend on semantic fields, not those wrapper objects.
 * Readiness/platform snapshots are immutable upstream producer values. */
export function useMemoizedStatusBarGatewayHealth({
  connectionState,
  copy,
  inferenceStatus,
  messagingRunning,
  messagingState,
  platforms,
  restarting
}: GatewayHealthPillInput) {
  const {
    backend,
    checking,
    connecting,
    messagingDegraded,
    messagingStopped,
    needsSetup,
    offline,
    ready,
    restarting: restartingCopy,
    unavailable
  } = copy

  return useMemo(
    () =>
      statusBarGatewayHealth({
        connectionState,
        copy: {
          backend,
          checking,
          connecting,
          messagingDegraded,
          messagingStopped,
          needsSetup,
          offline,
          ready,
          restarting: restartingCopy,
          unavailable
        },
        inferenceStatus,
        messagingRunning,
        messagingState,
        platforms,
        restarting
      }),
    [
      connectionState,
      backend,
      checking,
      connecting,
      messagingDegraded,
      messagingStopped,
      needsSetup,
      offline,
      ready,
      restartingCopy,
      unavailable,
      inferenceStatus,
      messagingRunning,
      messagingState,
      platforms,
      restarting
    ]
  )
}
