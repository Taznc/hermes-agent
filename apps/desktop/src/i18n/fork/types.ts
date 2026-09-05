// Fork-added translation key declarations.
//
// Upstream owns `../types.ts`; every key this fork adds is declared here and
// intersected into `Translations` at the single anchor in that file, so an
// upstream sync never has to merge two sets of additions into one interface
// body. Adding a fork key means editing this file only.

export interface ForkTranslations {
  boot: {
    errors: {
      gatewaySessionsStale: string
    }
    failure: {
      wsAuthTitle: string
      wsAuthDescription: string
      wsAuthHint: string
      openLogsFailed: string
    }
  }
  settings: {
    gateway: {
      secretStorageHintTitle: string
      secretStorageHintDesc: string
      secretStorageHintEnable: string
      secretStorageHintDismiss: string
      openLogsFailed: string
    }
    sessions: {
      /** Phase 2.12 — `sessions.rate_limit_default_recovery` (ask | resume_at_reset). */
      rateLimitRecoveryTitle: string
      rateLimitRecoveryDesc: string
      rateLimitRecoveryFailed: string
    }
  }
  commandCenter: {
    maintenance: {
      curatorLoadFailed: string
      memoryLoadFailed: string
      retry: string
      actionTailLost: string
    }
  }
  profiles: {
    switchToAgent: (profile: string, device: string) => string
    connectToAgent: (device: string) => string
    notConnected: string
    agentsHeading: string
    thisDevice: string
    sourceUnreachable: string
  }
  sidebar: {
    row: {
      /** Phase 2.12 — "rate limited" terminal sidebar status.
       *  `withTime` when resetAt is known, `unknown` otherwise — never
       *  fabricate a reset time. */
      rateLimited: {
        withTime: (time: string) => string
        unknown: string
      }
      /** Accessible name for the primary configured-model chip when the
       *  actually-served route for the latest turn matches (the common,
       *  no-mismatch case) — e.g. "Configured model: Claude". */
      providerConfigured: (family: string) => string
      /** Visible secondary text AND tooltip label shown only when the
       *  latest completed turn's actually-served provider differs from the
       *  configured one (e.g. after a rate-limit fallback) — e.g. "via
       *  Codex". */
      providerVia: (family: string) => string
      /** Accessible name for the chip when a mismatch is showing — e.g.
       *  "Configured model: Claude, currently served via Codex". */
      providerConfiguredVia: (configuredFamily: string, servedFamily: string) => string
    }
  }
  composer: {
    reconnectingBanner: string
    catchingUpNotice: string
    turnLostNotice: string
    turnLostRegenerate: string
  }
  assistant: {
    thread: {
      showEarlierFailed: string
      /** Phase 2.12 — rate-limit turn recovery (resetAt/fallbackAvailable). */
      rateLimit: {
        /** Plain-language failure message naming the provider/account. */
        message: (provider: string) => string
        /** Local human-readable reset time, when resetAt is present. */
        resetsAt: (time: string) => string
        /** Shown instead of resetsAt when resetAt is absent — never fabricate a time. */
        resetUnknown: string
        resumeAtReset: string
        makeDefault: string
        switchModelAndRetry: string
        configureFallback: string
        /** Small transcript/status note when the backend's mid-turn fallback
         *  already fixed the turn — never rendered as a failure card. */
        switchedNotice: (from: string, to: string) => string
        countdownLabel: (seconds: number) => string
        cancelCountdown: string
        jobScheduled: (time: string) => string
        jobCancel: string
        jobCancelFailed: string
        jobScheduleFailed: string
        jobDuplicate: string
        switchModelFailed: string
      }
      /** Self-improvement review row's expandable per-action detail list
       *  (ROADMAP.md Phase 1: Desktop transcript auditability). */
      review: {
        showDetails: string
        showDetailsWithFailures: (count: number) => string
        hideDetails: string
        failedReason: (message: string) => string
      }
    }
    tool: {
      /** Per-action memory titles, keyed by the tool's `action` argument — a
       *  search reads as "Searching memory", a write as "Saving a note", so
       *  the wait names what is actually happening instead of a fixed
       *  "Saving to memory" regardless of what the call does. */
      memoryActions: Record<
        'add' | 'search' | 'probe' | 'related' | 'reason' | 'contradict' | 'update' | 'remove' | 'list',
        { done: string; pending: string }
      >
    }
  }
  errors: {
    openLogsFailed: string
  }
}
