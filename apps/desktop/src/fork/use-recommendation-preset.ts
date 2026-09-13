import { useCallback, useEffect, useRef, useState } from 'react'

import {
  DEFAULT_RECOMMENDATION_PRESET,
  readRecommendationPreset,
  type RecommendationPreset,
  writeRecommendationPreset
} from './model-recommendation'

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

export interface RecommendationPresetPreference {
  /** Persist a deliberate user choice. Optimistic, with a guarded rollback. */
  choosePreset: (next: RecommendationPreset) => Promise<void>
  /** The preset to display. Meaningless until `presetReady`. */
  preset: RecommendationPreset
  /** False once a write was refused: the choice is live but session-local. */
  presetPersisted: boolean
  /** Whether the profile-scoped read has settled (or a user choice replaced it). */
  presetReady: boolean
  /** Awaits the initial read, then answers with the CURRENT intent. */
  resolvePreset: () => Promise<RecommendationPreset>
}

/**
 * THE preset state machine for one profile.
 *
 * Extracted from the surface because three async orderings decide whether the
 * user's persisted preference is honoured, and each of them silently sends the
 * WRONG policy to a paid router when it is not guarded:
 *
 *  - A click that lands before the initial `config.get` resolves must WAIT for
 *    it, not fall back to `balanced`. Disabling the trigger during the read
 *    would be worse than waiting: it makes the first click after mount a
 *    no-op, and there is nothing to show the user why.
 *  - A read that resolves after the user already picked, or after the profile
 *    changed underneath it, is stale intent and must stand down. Only the
 *    recommendation call had an epoch before; reads and writes need the same
 *    guard, keyed on BOTH the intent counter and the profile the request was
 *    issued for (`apps/desktop/AGENTS.md` — "Guard against the past").
 *  - Two writes in flight resolve in either order. Only the newest may repaint
 *    or roll back; a superseded write's failure must not drag a newer choice
 *    backwards, and must not claim the newer one was unsaved.
 *
 * `presetReady` is deliberately about the CURRENT profile: a profile swap
 * re-opens the unknown state rather than presenting the previous profile's
 * answer as this one's.
 */
export function useRecommendationPresetPreference(
  profile: string,
  request: RequestGateway
): RecommendationPresetPreference {
  const [state, setState] = useState<{ persisted: boolean; ready: boolean; value: RecommendationPreset }>({
    persisted: true,
    ready: false,
    value: DEFAULT_RECOMMENDATION_PRESET
  })

  // Newest intent wins. Bumped by every user choice AND every profile change,
  // so a response can be matched against the world it was issued for.
  const intentRef = useRef(0)
  const profileRef = useRef(profile)
  // The live value, readable synchronously by `resolvePreset` after the await
  // (React state would still be the pre-choice render's value).
  const valueRef = useRef<RecommendationPreset>(DEFAULT_RECOMMENDATION_PRESET)
  const readRef = useRef<null | Promise<unknown>>(null)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment): these refs track async intent/profile for staleness guards; no nanostore is mirrored
  useEffect(() => {
    const intent = ++intentRef.current
    profileRef.current = profile
    valueRef.current = DEFAULT_RECOMMENDATION_PRESET
    setState({ persisted: true, ready: false, value: DEFAULT_RECOMMENDATION_PRESET })

    const read = readRecommendationPreset({ profile, request }).then(({ persisted, value }) => {
      // Stale on either axis: a newer intent (user picked, or the profile
      // changed again) or a different profile than the one now mounted.
      if (intentRef.current !== intent || profileRef.current !== profile) {
        return
      }

      valueRef.current = value
      setState({ persisted, ready: true, value })
    })

    readRef.current = read

    return () => {
      // A superseded read must not answer a later `resolvePreset`.
      if (readRef.current === read) {
        readRef.current = null
      }
    }
  }, [profile, request])

  const resolvePreset = useCallback(async () => {
    const pending = readRef.current

    if (pending) {
      await pending
    }

    return valueRef.current
  }, [])

  const choosePreset = useCallback(
    async (next: RecommendationPreset) => {
      const intent = ++intentRef.current
      const forProfile = profileRef.current

      // Optimistic: the selector is a preference, so it paints immediately and
      // a refused write rolls it back rather than asserting a false save.
      valueRef.current = next
      setState({ persisted: true, ready: true, value: next })

      const isCurrent = () => intentRef.current === intent && profileRef.current === forProfile

      try {
        await writeRecommendationPreset({ profile: forProfile, request, value: next })

        if (isCurrent()) {
          setState({ persisted: true, ready: true, value: next })
        }
      } catch {
        // A superseded write's failure says nothing about the newer choice.
        if (!isCurrent()) {
          return
        }

        valueRef.current = next
        setState({ persisted: false, ready: true, value: next })
      }
    },
    [request]
  )

  return {
    choosePreset,
    preset: state.value,
    presetPersisted: state.persisted,
    presetReady: state.ready,
    resolvePreset
  }
}
