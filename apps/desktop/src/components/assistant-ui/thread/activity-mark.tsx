import { type FC, useMemo } from 'react'

import { StatusPulse } from '@/components/ui/status-pulse'
import { type Contribution, useContributions } from '@/contrib'
import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { useMediaQuery } from '@/hooks/use-media-query'
import { THREAD_ACTIVITY_AREA, type ThreadActivityContribution, type ThreadActivityState } from '@/lib/thread-activity'

/**
 * The transcript's active-work mark. Renders the registered `thread.activity`
 * contribution when one exists, and the core dither pulse when none does.
 *
 * Two properties this slot must never trade away, because the row around it is
 * what makes the app honest about working:
 *
 * 1. The mark is decoration. The row owns `role="status"` and the accessible
 *    label, so this is `aria-hidden` either way — a plugin cannot take the
 *    status line away from a screen reader by claiming the area.
 * 2. A plugin that throws loses only the mark. `ContribBoundary` catches it
 *    and the hint + elapsed clock beside it keep rendering.
 */
/**
 * What the host rows pass in. `reducedMotion` is deliberately absent: the slot
 * reads the OS preference itself, so no caller can forget it and hand a plugin
 * a state that says motion is fine when it isn't.
 */
export type ThreadActivityMarkProps = Omit<ThreadActivityState, 'reducedMotion'>

export const ThreadActivityMark: FC<ThreadActivityMarkProps> = state => {
  const contributions = useContributions(THREAD_ACTIVITY_AREA)
  // Live, not read-once: the OS setting can flip mid-turn and a long wait is
  // exactly when a user reaches for it.
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')

  // First registration wins, matching `transcript.directives`. One indicator,
  // never a stack of them.
  const match: Contribution | undefined = contributions[0]
  const render = (match?.data as ThreadActivityContribution | undefined)?.render

  const { elapsedSeconds, hint, phase, slot } = state

  // Stable component identity for ContribRender (which mounts this AS a
  // component): a fresh closure per render would remount the mark — and
  // restart its animation — on every tick of the elapsed clock.
  const renderMark = useMemo(
    () => (render ? () => render({ elapsedSeconds, hint, phase, reducedMotion, slot }) : null),
    [render, elapsedSeconds, hint, phase, reducedMotion, slot]
  )

  if (!match || !renderMark) {
    return (
      <StatusPulse
        aria-hidden="true"
        className="dither inline-block size-3 rounded-[2px] text-midground/80"
        kind="opacity"
      />
    )
  }

  return (
    <span aria-hidden="true" className="inline-flex shrink-0 items-center">
      <ContribBoundary id={match.id} variant="chip">
        <ContribRender render={renderMark} />
      </ContribBoundary>
    </span>
  )
}
