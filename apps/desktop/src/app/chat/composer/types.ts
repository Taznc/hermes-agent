import type { ReactNode } from 'react'

import type { SubmitTextOptions } from '@/app/session/hooks/use-prompt-actions/utils'
import type { HermesGateway } from '@/hermes'
import type { ComposerAttachment } from '@/store/composer'

import type { DroppedFile } from '../hooks/use-composer-actions'

export interface ContextSuggestion {
  text: string
  display: string
  meta?: string
}

export interface QuickModelOption {
  provider: string
  providerName: string
  model: string
}

export interface ChatBarState {
  model: {
    model: string
    provider: string
    canSwitch: boolean
    loading?: boolean
    quickModels?: QuickModelOption[]
    /** Reused status-bar dropdown (built with gateway + selectModel upstream). */
    modelMenuContent?: ReactNode
    // >>> FORK ANCHOR: composer-model-recommendation <<<
    /** Fork: the manual model-recommendation surface. A RENDER FUNCTION, not a
     *  node, because the two halves live on opposite sides of this boundary:
     *  gateway routing (profile, request, session-aware `selectModel`) belongs
     *  to the ChatView's owner, exactly like `modelMenuContent`; the live draft
     *  and attachment chips belong to the composer and are handed back here.
     *  Absent (older/unwired owner) renders nothing at all. */
    recommendRender?: (ctx: ComposerRecommendContext) => ReactNode
  }
  tools: { enabled: boolean; label: string; suggestions?: ContextSuggestion[] }
  voice: { enabled: boolean; active: boolean }
}

// >>> FORK ANCHOR: composer-model-recommendation <<<
/** What the composer contributes to a recommendation request: the LIVE draft
 *  (a getter — reading a captured string would evaluate a stale draft, and a
 *  setter would let this surface mutate what the user is typing) and the
 *  attachment chips whose metadata may accompany it. */
export interface ComposerRecommendContext {
  attachments: readonly ComposerAttachment[]
  disabled: boolean
  getDraft: () => string
}

export interface ChatBarProps {
  busy: boolean
  disabled: boolean
  focusKey?: string | null
  maxRecordingSeconds?: number
  state: ChatBarState
  gateway?: HermesGateway | null
  queueSessionKey?: string | null
  sessionId?: string | null
  /** The STORED session id (survives runtime-id churn across a reconnect) —
   *  keys the reconnect catch-up / turn-lost notices, which live in
   *  session-states.ts keyed by stored id, not runtime id. */
  storedSessionId?: string | null
  cwd?: string | null
  onCancel: () => Promise<void> | void
  onAddContextRef?: (refText: string, label?: string, detail?: string) => void
  onAddUrl?: (url: string) => void
  onAttachImageBlob?: (blob: Blob) => Promise<boolean | void> | boolean | void
  onAttachDroppedItems?: (candidates: DroppedFile[]) => Promise<boolean | void> | boolean | void
  /** Pasted GitHub PR-comment deep link → structured review attachment.
   *  Returns true when the paste was consumed as an attachment. */
  onAttachPrCommentUrl?: (url: string) => boolean
  onPasteClipboardImage?: (opts?: { silent?: boolean }) => Promise<boolean> | void
  onPickFiles?: () => void
  onPickFolders?: () => void
  onPickImages?: () => void
  onReload?: (parentId: string | null) => Promise<void>
  onRemoveAttachment?: (id: string) => void
  onSteer?: (text: string) => Promise<boolean> | boolean
  onSubmit: (value: string, options?: SubmitTextOptions) => Promise<boolean> | boolean
  onTranscribeAudio?: (audio: Blob) => Promise<string>
}

export type VoiceStatus = 'idle' | 'recording' | 'transcribing'

export interface VoiceActivityState {
  elapsedSeconds: number
  level: number
  status: VoiceStatus
}
