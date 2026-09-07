import type { ForkTranslations } from './types'

// English values for the fork-added keys declared in ./types.ts. Merged into
// `en` at the single anchor in ../en.ts. Every other locale inherits these
// through defineLocale()'s English fallback unless it ships its own fork
// translations (see ./ar.ts, ./ja.ts, ./zh.ts, ./zh-hant.ts).

export const forkEn: ForkTranslations = {
  boot: {
    errors: {
      gatewaySessionsStale: 'Reconnected, but sessions/settings could not refresh. Some lists may be stale.'
    },
    failure: {
      wsAuthTitle: 'Sign-in required',
      wsAuthDescription:
        "Hermes is running and responding normally — only the live connection's access credential was rejected. Nothing here deletes your chats or settings.",
      wsAuthHint:
        'Open the link you were given to reach this Hermes instance again (it carries a fresh credential), or ask whoever set it up for a new one.',
      openLogsFailed: 'Could not open the logs folder'
    }
  },
  settings: {
    gateway: {
      secretStorageHintTitle: 'Stored without OS keychain encryption',
      secretStorageHintDesc:
        'Gateway tokens and sign-in credentials are stored as plain files readable only by your user account. Enable OS keychain encryption below for stronger protection.',
      secretStorageHintEnable: 'Enable encryption',
      secretStorageHintDismiss: 'Dismiss',
      openLogsFailed: 'Could not open the logs folder'
    },
    sessions: {
      rateLimitRecoveryTitle: 'When a turn hits a rate limit',
      rateLimitRecoveryDesc:
        'Ask each time (default) always shows the failure card and lets you choose. Resume automatically starts a brief, cancelable countdown before scheduling a resume.',
      rateLimitRecoveryFailed: 'Could not update the rate-limit recovery preference'
    }
  },
  commandCenter: {
    maintenance: {
      curatorLoadFailed: 'Could not load curator status',
      memoryLoadFailed: 'Could not load memory data',
      retry: 'Retry',
      actionTailLost: 'Lost track of this task — view in activity rail'
    }
  },
  profiles: {
    switchToAgent: (profile: string, device: string) => `Switch to ${profile} on ${device}`,
    connectToAgent: (device: string) => `Connect to ${device}`,
    notConnected: 'Not connected yet',
    agentsHeading: 'Agents & connections',
    thisDevice: 'This device',
    sourceUnreachable: 'Unreachable'
  },
  sidebar: {
    row: {
      rateLimited: {
        withTime: time => `Rate limited — retry at ${time}`,
        unknown: 'Rate limited — reset time unknown'
      },
      providerConfigured: family => `Configured model: ${family}`,
      providerVia: family => `via ${family}`,
      providerConfiguredVia: (configuredFamily, servedFamily) =>
        `Configured model: ${configuredFamily}, currently served via ${servedFamily}`
    }
  },
  composer: {
    reconnectingBanner: 'Reconnecting to Hermes — you can keep reading and typing.',
    catchingUpNotice: 'Reconnected — catching up…',
    turnLostNotice: 'This turn may not have completed during the disconnect.',
    turnLostRegenerate: 'Regenerate',
    recommend: {
      trigger: 'Recommend',
      presetLabel: 'Recommendation preference',
      presets: {
        balanced: 'Balanced',
        save_codex: 'Save Codex',
        best_quality: 'Best quality'
      },
      presetDescriptions: {
        balanced: 'Balanced: good results at a reasonable cost.',
        save_codex: 'Save Codex: prefers other routes so Codex usage lasts longer.',
        best_quality: 'Best quality: picks the strongest route, whatever it costs.'
      },
      presetLoading: 'Loading your preference…',
      presetUnsaved: 'Not saved on this backend — applies to this session only.',
      resultsLabel: 'Model recommendations',
      privacy:
        'Checks your current draft and attachment names only. Conversation history, file contents, and project files are never sent.',
      pending: 'Checking…',
      apply: 'Apply',
      applyUnconfirmed: 'Not applied yet — confirm the switch, or pick a model manually.',
      retry: 'Try again',
      failed: 'The recommendation check failed.',
      unavailable: 'No recommendation is available. Configure a recommendation router in settings to enable this.',
      unsupported: 'This Hermes backend does not support recommendations.',
      stale: 'Your draft changed, so these recommendations no longer apply.',
      refresh: 'Check again',
      emptyDraft: 'Write a draft first, then check.',
      draftTooLong: 'This draft is too long to check.',
      tooManyAttachments: 'Too many attachments to check (32 maximum).',
      attachmentUnsupported: 'An attachment name is too long to check.',
      availability: {
        failed: 'availability check failed',
        fresh: 'live',
        stale: 'stale availability',
        unavailable: 'unavailable',
        unsupported: 'availability unknown'
      },
      limitReached: 'limit reached',
      notAllowed: 'not available on your plan'
    }
  },
  assistant: {
    thread: {
      showEarlierFailed: 'Could not load earlier messages',
      workComplete: 'Work complete',
      workNeedsAttention: 'Work needs attention',
      rateLimit: {
        message: provider => `${provider || 'The provider'} is rate limiting this account.`,
        resetsAt: time => `Retry at ${time}`,
        resetUnknown: 'Reset time unknown',
        resumeAtReset: 'Resume at reset',
        makeDefault: 'Make this the default',
        switchModelAndRetry: 'Switch model & retry',
        configureFallback: 'Configure automatic fallback…',
        switchedNotice: (from, to) => `Switched from ${from} to ${to} and continued`,
        countdownLabel: seconds => `Resuming in ${seconds}s… Cancel`,
        cancelCountdown: 'Cancel',
        jobScheduled: time => `Resume scheduled for ${time}`,
        jobCancel: 'Cancel resume',
        jobCancelFailed: 'Could not cancel the scheduled resume',
        jobScheduleFailed: 'Could not schedule the resume',
        jobDuplicate: 'A resume is already scheduled for this turn',
        switchModelFailed: 'Could not switch models'
      },
      review: {
        showDetails: 'Show details',
        showDetailsWithFailures: count => (count === 1 ? 'Show details (1 failed)' : `Show details (${count} failed)`),
        hideDetails: 'Hide details',
        hideRecordDetails: 'Hide record details',
        legacyDetail: 'Details are unavailable from this older Hermes version.',
        target: target => ({ memory: 'Memory', skill: 'Skill', user: 'User profile' })[target] ?? 'Review item',
        operation: operation =>
          ({ add: 'Add', create: 'Create', edit: 'Edit', patch: 'Update', remove: 'Remove', replace: 'Replace' })[
            operation
          ] ?? 'Update',
        recordSummary: (target, operation, state) => `${target} · ${operation} · ${state}`,
        showRecordDetails: target => `Show ${target} review details`,
        state: state =>
          ({ completed: 'Completed', declined: 'Declined', failed: 'Failed', no_op: 'No change', skipped: 'Skipped' })[
            state
          ] ?? 'Completed'
      }
    },
    tool: {
      memoryActions: {
        add: { done: 'Saved a note', pending: 'Saving a note' },
        search: { done: 'Searched memory', pending: 'Searching memory' },
        probe: { done: 'Recalled memory', pending: 'Recalling memory' },
        related: { done: 'Found related memory', pending: 'Finding related memory' },
        reason: { done: 'Reasoned across memory', pending: 'Reasoning across memory' },
        contradict: { done: 'Checked memory for conflicts', pending: 'Checking memory for conflicts' },
        update: { done: 'Updated memory', pending: 'Updating memory' },
        remove: { done: 'Removed from memory', pending: 'Removing from memory' },
        list: { done: 'Listed memory', pending: 'Listing memory' }
      }
    }
  },
  errors: {
    openLogsFailed: 'Could not open the logs folder'
  }
}
