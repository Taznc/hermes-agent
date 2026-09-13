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
      /** Browser build: the host owns no connection registry, so this page can
       *  only ever describe the one backend it is served by. Says so plainly
       *  instead of rendering an empty "unavailable" page that reads like a
       *  bug the user could fix. */
      singleBackendTitle: string
      /** With the backend's host, when the connection descriptor has one. */
      singleBackendDesc: (host: string) => string
      /** Host-free variant — used before the descriptor lands, or when its
       *  baseUrl does not parse. Never paints "on undefined". */
      singleBackendDescNoHost: string
      singleBackendDocsLink: string
    }
    sessions: {
      /** Phase 2.12 — `sessions.rate_limit_default_recovery` (ask | resume_at_reset). */
      rateLimitRecoveryTitle: string
      rateLimitRecoveryDesc: string
      rateLimitRecoveryFailed: string
    }
  }
  rightSidebar: {
    terminalUnavailableTitle: string
    terminalUnavailableBody: string
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
      /** Inverse archive labels for rows rendered by the Archived filter. */
      unarchive: string
      unarchiveSession: string
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
  desktop: {
    /** Confirmation/failure copy for restoring a row from the Archived filter. */
    unarchived: string
    unarchiveFailed: string
  }
  composer: {
    reconnectingBanner: string
    catchingUpNotice: string
    turnLostNotice: string
    turnLostRegenerate: string
    /** Manual, advisory model-recommendation flow (`model_recommendation.get`). */
    recommend: {
      trigger: string
      presetLabel: string
      presets: { balanced: string; save_codex: string; best_quality: string }
      /** Concise description of what each preset optimizes for. */
      presetDescriptions: { balanced: string; save_codex: string; best_quality: string }
      /** Shown INSTEAD of a selection while the profile-scoped read is pending —
       *  painting a default would claim a stored choice that was never read. */
      presetLoading: string
      presetUnsaved: string
      resultsLabel: string
      /** States the FINAL privacy boundary: draft + attachment metadata only. */
      privacy: string
      pending: string
      apply: string
      /** Shown when the selection path did not apply outright — usually a
       *  pending expensive-model confirmation, never a claim of failure. */
      applyUnconfirmed: string
      /** The selection path FAILED. Distinct from `applyUnconfirmed`: there is
       *  nothing left to confirm, and the previous model is still in use
       *  because the switch was rolled back (or never took effect). */
      applyFailed: string
      /** The switch failed AND the gateway refused the compensation, so the
       *  backend may be left on the new model. Never claims a rollback that
       *  did not happen — points the user at the model menu to verify. */
      applyUnrestored: string
      retry: string
      failed: string
      unavailable: string
      unsupported: string
      /** Results no longer describe the live draft/attachments/preset. */
      stale: string
      refresh: string
      emptyDraft: string
      draftTooLong: string
      tooManyAttachments: string
      attachmentUnsupported: string
      /** Every reported state is rendered, `fresh` included: an unreported
       *  availability and a checked-live one must not look the same. */
      availability: { failed: string; fresh: string; stale: string; unavailable: string; unsupported: string }
      /** The backend reported this route's quota is exhausted. */
      limitReached: string
      /** The backend reported this route is not permitted right now. */
      notAllowed: string
    }
  }
  assistant: {
    thread: {
      showEarlierFailed: string
      workComplete: string
      workNeedsAttention: string
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
        hideRecordDetails: string
        legacyDetail: string
        target: (target: string) => string
        operation: (operation: string) => string
        recordSummary: (target: string, operation: string, state: string) => string
        showRecordDetails: (target: string) => string
        state: (state: string) => string
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
