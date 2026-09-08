import { useStore } from '@nanostores/react'
import type * as React from 'react'

import { type Translations, useI18n } from '@/i18n'
import { fmtClock } from '@/lib/time'
import { useStoreSelector } from '@/lib/use-session-slice'
import { cn } from '@/lib/utils'
import { $sessionColorById, sessionColorFor } from '@/store/session-color'
import { $rateLimitedSessionIds, $sessionDotStateById, type SessionDotState } from '@/store/session-dot-state'
import type { SessionInfo } from '@/types/hermes'

// A pure lookup table: each state maps to its glyph, color, aria-label, and
// title. No priority resolution here — $sessionDotStateById already picked one.
// Label/title resolve from sidebar.row translations, keyed by name.
type DotVariant = {
  ariaLabel?: (r: Translations['sidebar']['row']) => string
  /** Text color the glyph draws in (SVGs stroke/fill `currentColor`). */
  className: string
  glyph: React.ReactNode
  role?: 'status'
  title?: (r: Translations['sidebar']['row']) => string
}

// DESCRIPTIVE GLYPHS, not bare dots: each state is a small self-explanatory
// icon — ? for a blocking question, a clock for a quiet turn, a checkmark for
// an unseen finish — so the sidebar reads without a legend. All 13px
// (0.8125rem) inside the 14px lead cell, all drawn in `currentColor` so the
// wrapper's text color is the single color knob (theme tokens included).
// Still no continuous motion here — liveness is the row's charging bar.
const GLYPH_SIZE = '0.8125rem'

const svgProps = {
  'aria-hidden': true as const,
  fill: 'none',
  height: GLYPH_SIZE,
  viewBox: '0 0 16 16',
  width: GLYPH_SIZE
}

const GLYPHS: Record<Exclude<SessionDotState, 'idle'>, React.ReactNode> = {
  // Circled question mark — a clarify/approval is waiting on an answer.
  'needs-input': (
    <svg {...svgProps}>
      <circle cx="8" cy="8" r="6.2" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M6.3 6.4c.2-1 1-1.6 1.9-1.5.9 0 1.7.7 1.7 1.6 0 1.2-1.7 1.4-1.7 2.6"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.4"
      />
      <circle cx="8.1" cy="11.4" fill="currentColor" r=".9" />
    </svg>
  ),
  // Ringed core — the turn is producing. The dashed ring suggests rotation
  // without moving; the row's charging bar carries the actual motion.
  working: (
    <svg {...svgProps}>
      <circle cx="8" cy="8" fill="currentColor" r="3" />
      <circle
        cx="8"
        cy="8"
        opacity=".75"
        r="6"
        stroke="currentColor"
        strokeDasharray="7 4.5"
        strokeLinecap="round"
        strokeWidth="1.3"
      />
    </svg>
  ),
  // Clock — still authoritatively running, but quiet past the watchdog
  // window: time is passing with nothing arriving.
  stalled: (
    <svg {...svgProps}>
      <circle cx="8" cy="8" opacity=".8" r="6.2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M8 4.8V8l2.2 1.6" stroke="currentColor" strokeLinecap="round" strokeWidth="1.4" />
    </svg>
  ),
  // Detached satellite — a background process/delegation outlived the turn:
  // the session is open, with work orbiting outside it.
  background: (
    <svg {...svgProps}>
      <circle cx="8" cy="8" r="6.2" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="8" cy="8" fill="currentColor" opacity=".55" r="2" />
      <circle cx="12.6" cy="4.2" fill="currentColor" r="1.6" />
    </svg>
  ),
  // Filled check — finished while you were looking elsewhere. The check is
  // cut from the sidebar surface so it reads at 13px in both schemes.
  unread: (
    <svg {...svgProps}>
      <circle cx="8" cy="8" fill="currentColor" r="6.5" />
      <path
        d="M5.2 8.2l1.9 1.9 3.7-4"
        stroke="var(--background)"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
      />
    </svg>
  ),
  // Hourglass — waiting out a rate-limit window. Orange, never amber, so it
  // can't be confused with a needs-input prompt.
  'rate-limited': (
    <svg {...svgProps}>
      <path
        d="M5 2.5h6M5 13.5h6M5.5 2.5c0 5 5 5 5 11M10.5 2.5c0 5-5 5-5 11"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.4"
      />
    </svg>
  ),
  // Pencil — nothing has ever run here; the chat is still being written.
  draft: (
    <svg {...svgProps}>
      <path
        d="M3 13l.8-3L11 2.8a1.4 1.4 0 012 2L5.8 12l-2.8 1z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.3"
      />
    </svg>
  )
}

const DOT_VARIANTS: Record<SessionDotState, DotVariant> = {
  // Amber — a clarify/approval is blocking the turn. The one "act now" color,
  // and the only state the user is required to do something about.
  'needs-input': {
    ariaLabel: r => r.needsInput,
    className: 'text-amber-500',
    glyph: GLYPHS['needs-input'],
    role: 'status',
    title: r => r.waitingForAnswer
  },
  // Accent — the turn is running. The row's charging bar carries the motion.
  working: {
    ariaLabel: r => r.sessionRunning,
    className: 'text-(--ui-accent)',
    glyph: GLYPHS.working,
    role: 'status'
  },
  // Accent clock — still authoritatively running, but nothing has arrived for
  // the watchdog window. Same color as working because it IS working.
  stalled: {
    ariaLabel: r => r.sessionRunning,
    className: 'text-(--ui-accent)',
    glyph: GLYPHS.stalled,
    role: 'status',
    title: r => r.sessionRunning
  },
  // Muted — a terminal(background=true) process outlived the turn. Grey, not
  // accent: open and running, but the model is not working.
  background: {
    ariaLabel: r => r.backgroundRunning,
    className: 'text-(--ui-text-tertiary)',
    glyph: GLYPHS.background,
    role: 'status',
    title: r => r.backgroundRunning
  },
  // Emerald — the turn finished while the user was looking elsewhere. The
  // color is theme-derived (`--ui-success`, a success green rotated toward the
  // accent) so eight finished checks can't sit in the sidebar fighting a
  // palette they don't belong to.
  unread: {
    ariaLabel: r => r.finishedUnread,
    className: 'text-(--ui-success)',
    glyph: GLYPHS.unread,
    role: 'status',
    title: r => r.finishedUnread
  },
  // Amber-adjacent but distinct: orange, so a rate-limited session never gets
  // confused with an amber needs-input prompt. Label/title are resolved
  // dynamically in SessionStatusDot (need the resolved reset time), so this
  // entry carries only the visual — its ariaLabel/title functions here are
  // unused fallbacks for callers that read the table directly.
  'rate-limited': {
    ariaLabel: r => r.rateLimited.unknown,
    className: 'text-orange-500',
    glyph: GLYPHS['rate-limited'],
    role: 'status',
    title: r => r.rateLimited.unknown
  },
  // The faintest ink the app has — nothing has ever run here.
  draft: {
    ariaLabel: r => r.draftSession,
    className: 'text-(--ui-text-quaternary)',
    glyph: GLYPHS.draft,
    title: r => r.draftSession
  },
  // Settled: the project color when there is one, else the faintest filled
  // grey. Every session shows SOME mark — a row with nothing in the lead slot
  // reads as broken next to its neighbours, so "no color" falls back to the
  // quietest ink rather than to an invisible dot. Stays a tiny dot on
  // purpose: settled sessions should be the quietest thing in the list.
  idle: {
    className: 'size-1 rounded-full bg-(--ui-text-quaternary)',
    glyph: null
  }
}

/** The mark a state paints, for surfaces that describe a status rather than
 *  render a session — the sidebar's status filter, the agents view. Idle
 *  renders its quiet fallback dot (no project color to inherit here). */
export function SessionStatusMark({ className, state }: { className?: string; state: SessionDotState }) {
  const variant = DOT_VARIANTS[state]

  if (state === 'idle') {
    return <span aria-hidden="true" className={cn(variant.className, className)} />
  }

  return (
    <span aria-hidden="true" className={cn('inline-flex shrink-0', variant.className, className)}>
      {variant.glyph}
    </span>
  )
}

export interface SessionStatusDotProps {
  /** The STORED session id — the key every live-state atom (working /
   *  attention / stalled / unread / background) is keyed by, on BOTH surfaces:
   *  the sidebar row's `session.id` and a pane tile's `storedSessionId` are the
   *  same stored id (`$workingSessionIds` et al. map `storedSessionId`).
   *
   *  Null on a new chat that has yet to reach the backend — no id to key by,
   *  and no turn behind it, which is the draft state by definition. */
  storedSessionId: null | string
  /** The session row for color resolution — recents OR the project tree. Both
   *  call sites already hold it; passing it lets the idle dot inherit the
   *  project color even for a session older than the paginated recents page
   *  (which has no `$sessionColorById` entry). */
  session?: null | SessionInfo
  /** TUI-style tree stem for a branched session (`└─ ` / `├─ `). */
  branchStem?: string
  /** Applied to the OUTER wrapper (stem + dot) — e.g. hover-fade on the
   *  reorder handle. */
  className?: string
}

/**
 * SESSION STATUS DOT — the ONE primitive the sidebar row, the pane tabs, and
 * the session switcher render, so a session's status can never disagree
 * between surfaces. It resolves everything itself from the stored session id:
 * the live state (via `$sessionDotStateById`, already reduced to one mutually
 * exclusive answer) and the color (override → project, via `sessionColorFor`).
 * An idle session shows its project color; the active states own the mark with
 * their semantic glyph so an attention cue is never masked by the tint.
 */
export function SessionStatusDot({ storedSessionId, session, branchStem, className }: SessionStatusDotProps) {
  const { t } = useI18n()
  const r = t.sidebar.row

  // Subscribe to the shared color map for reactivity; sessionColorFor falls
  // back to the resolver for a session outside the recents page.
  useStore($sessionColorById)
  const color = sessionColorFor(session) ?? null

  // Selector, not a plain useStore: the map is rebuilt whenever any session's
  // status changes, but a given dot only repaints when ITS OWN state flips.
  const dotState = useStoreSelector($sessionDotStateById, states =>
    storedSessionId ? (states[storedSessionId] ?? 'idle') : 'draft'
  )

  // Rate-limited (Phase 2.12): the label needs the resolved reset time, which
  // the static DOT_VARIANTS table can't carry — read it directly rather than
  // widening the table to accept per-call args.
  const rateLimitedResetAt = useStoreSelector($rateLimitedSessionIds, entries =>
    storedSessionId ? entries[storedSessionId]?.resetAt : undefined
  )

  const variant = DOT_VARIANTS[dotState]

  const rateLimitedLabel =
    dotState === 'rate-limited'
      ? rateLimitedResetAt !== undefined
        ? r.rateLimited.withTime(fmtClock.format(new Date(rateLimitedResetAt * 1000)))
        : r.rateLimited.unknown
      : null

  return (
    <span className={cn('flex items-center gap-0.5', className)}>
      {branchStem ? (
        <span aria-hidden className="shrink-0 font-mono text-[0.625rem] leading-none text-(--ui-text-quaternary)">
          {branchStem}
        </span>
      ) : null}
      {dotState === 'idle' ? (
        // Rendered even with no color to paint: an empty dot of the same size
        // keeps every row's title on one left edge, so a session finishing
        // can't shift the list under the pointer.
        <span aria-hidden="true" className={variant.className} style={color ? { backgroundColor: color } : undefined} />
      ) : (
        <span
          aria-label={rateLimitedLabel ?? variant.ariaLabel?.(r)}
          className={cn('inline-flex shrink-0', variant.className)}
          role={variant.role}
          title={rateLimitedLabel ?? variant.title?.(r)}
        >
          {variant.glyph}
        </span>
      )}
    </span>
  )
}
