import { type RefObject, useLayoutEffect, useRef } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { SLASH_COMMAND_RE } from '@/lib/chat-runtime'
import { triggerHaptic } from '@/lib/haptics'
import { hasClarifyRequest, skipClarifyRequest } from '@/store/clarify'
import { clearSessionDraft, type ComposerAttachment } from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import { enqueueQueuedPrompt, type QueuedPromptEntry } from '@/store/composer-queue'
import { hasMcpSetupRequest, skipMcpSetupRequest } from '@/store/mcp-setup'
import { hasBlockingPromptRequest } from '@/store/prompts'

import { cloneAttachments, type QueueEditState } from '../composer-utils'
import { onComposerSubmitRequest } from '../focus'
import { pathifyRefs } from '../path-refs'
import { composerPlainText } from '../rich-editor'
import { useComposerScope, useComposerSurfaceId } from '../scope'
import type { ChatBarProps } from '../types'

interface UseComposerSubmitArgs {
  activeQueueSessionKey: string | null
  activeQueueSessionKeyRef: RefObject<string | null>
  attachments: ComposerAttachment[]
  busy: boolean
  compacting: boolean
  clearDraft: () => void
  disabled: boolean
  draftRef: RefObject<string>
  drainNextQueued: () => Promise<boolean>
  editorRef: RefObject<HTMLDivElement | null>
  exitQueuedEdit: (action: 'cancel' | 'save') => boolean
  focusInput: () => void
  inputDisabled: boolean
  loadIntoComposer: (text: string, attachments: ComposerAttachment[]) => void
  onCancel: ChatBarProps['onCancel']
  onSteer: ChatBarProps['onSteer']
  onSubmit: ChatBarProps['onSubmit']
  queueCurrentDraft: () => boolean
  queueEdit: QueueEditState | null
  queuedPrompts: QueuedPromptEntry[]
  sessionId: string | null | undefined
  setComposerText: (value: string) => void
  stashAt: (scope: string | null, text?: string, attachments?: ComposerAttachment[]) => void
}

/**
 * The composer's submit engine — the orchestration seam where the draft and
 * queue meet. `submitDraft` is the one decision tree (queue-edit save · slash-
 * now-while-busy · queue · drain · send · stop); `dispatchSubmit` is the shared
 * send-with-restore primitive (re-loads + re-stashes the draft if the gateway
 * rejects, so nothing is ever lost); `steerDraft` redirects the live turn. Reads
 * the draft + queue APIs; owns no state of its own beyond the stable
 * external-submit listener ref.
 */
export function useComposerSubmit({
  activeQueueSessionKey,
  activeQueueSessionKeyRef,
  attachments,
  busy,
  compacting,
  clearDraft,
  disabled,
  draftRef,
  drainNextQueued,
  editorRef,
  exitQueuedEdit,
  focusInput,
  inputDisabled,
  loadIntoComposer,
  onCancel,
  onSteer,
  onSubmit,
  queueCurrentDraft,
  queueEdit,
  queuedPrompts,
  sessionId,
  setComposerText,
  stashAt
}: UseComposerSubmitArgs) {
  const paneVisible = usePaneVisible()
  const scope = useComposerScope()
  const surfaceId = useComposerSurfaceId()

  // Shared send primitive: fire onSubmit, and if the gateway rejects (accepted
  // === false) or throws, re-load + re-stash the draft so the words survive.
  const dispatchSubmit = (text: string, attachments?: ComposerAttachment[], displayKind?: 'hidden') => {
    const submittedScope = activeQueueSessionKeyRef.current
    const submittedAttachments = attachments ?? []

    const restore = () => {
      loadIntoComposer(text, submittedAttachments)
      // Use the scope captured at dispatch, not whatever session is focused
      // now — the gateway can reject well after the user has switched away,
      // and re-stashing into the currently-focused session would overwrite
      // its draft with the rejected text from a different session (#54527).
      stashAt(submittedScope, text, submittedAttachments)
    }

    void Promise.resolve(
      attachments
        ? onSubmit(text, { attachments, composerScope: submittedScope, ...(displayKind ? { displayKind } : {}) })
        : onSubmit(text, { composerScope: submittedScope, ...(displayKind ? { displayKind } : {}) })
    )
      .then(accepted => void (accepted === false ? restore() : clearSessionDraft(submittedScope)))
      .catch(restore)
  }

  // External "submit this prompt" requests (e.g. the review pane's agent-ship
  // button) route through the same send path. Match both the composer target
  // and the exact visible surface captured at click time — every tile stays
  // mounted, and a session can be rendered in more than one pane.
  const dispatchSubmitRef = useRef(dispatchSubmit)
  dispatchSubmitRef.current = dispatchSubmit

  useLayoutEffect(
    () =>
      onComposerSubmitRequest(({ surfaceId: requestedSurfaceId, target, text, displayKind }) => {
        if (
          target === scope.target &&
          surfaceId !== null &&
          requestedSurfaceId === surfaceId &&
          paneVisible &&
          !inputDisabled
        ) {
          dispatchSubmitRef.current(text, undefined, displayKind)
        }
      }),
    [inputDisabled, paneVisible, scope.target, surfaceId]
  )

  const submitDraft = () => {
    if (disabled) {
      return
    }

    // Source the text from the DOM editor, not React state. The AUI composer
    // state (`draft`) and the derived `hasComposerPayload` lag the DOM by a
    // render, so on fast typing or IME composition the final keystroke(s) may
    // not have synced yet — reading state here drops the message (Enter looks
    // like it does nothing; typing a trailing space only "fixes" it because the
    // extra input event forces a state sync). draftRef is updated on every
    // input event; refresh it from the editor once more to also cover an
    // in-flight keystroke that hasn't fired its input event yet.
    const editor = editorRef.current

    if (editor) {
      const domText = composerPlainText(editor)

      if (domText !== draftRef.current) {
        draftRef.current = domText
        setComposerText(domText)
      }
    }

    // A path that never got its committing space (`@apps/desktop/` left by a Tab
    // descend, then Enter) is still the reference the user picked — promote it
    // on the way out so it attaches instead of submitting as inert text.
    const text = pathifyRefs(draftRef.current)
    const payloadPresent = text.trim().length > 0 || attachments.length > 0

    // A clarify card parked on this session owns the turn: the agent is blocked
    // inside its tool batch waiting on `clarify.respond`, so a follow-up routed
    // through steer/queue sits undelivered until the clarify's own timeout
    // (default 5 min) — the message looks sent and nothing happens. Typing a
    // real message instead of picking an option IS the answer "none of these":
    // skip the question so the tool returns, then route the words normally.
    const clarifyPending = !queueEdit && hasClarifyRequest(sessionId)

    // Same deal for a pending MCP setup card: the agent is blocked on
    // mcp.setup.respond, so a typed message declines the card and rides on.
    if (payloadPresent && !queueEdit && hasMcpSetupRequest(sessionId)) {
      void skipMcpSetupRequest(sessionId)
    }

    // Approval / sudo / secret prompts also park the turn inside a tool batch,
    // but typing CANNOT answer them (no message text approves a command or
    // supplies a password), so there is no skip-and-steer path: a steer would
    // sit undelivered behind the blocked prompt, and stopping the turn to force
    // it through resolves the prompt to empty and ends the turn as "Operation
    // interrupted." — the message looks eaten. Queue the words as the next turn
    // instead; the prompt stays answerable and the queue drains on settle.
    const blockingPrompt = !queueEdit && hasBlockingPromptRequest(sessionId)

    // Whether submitDraft will actually route through steerDraft below — kept
    // in sync with that branch's own condition so the clarify-release decision
    // (right below) agrees with what's really about to happen.
    const willSteerClarify =
      clarifyPending &&
      busy &&
      !!onSteer &&
      !compacting &&
      !blockingPrompt &&
      !attachments.length &&
      !!text.trim() &&
      !SLASH_COMMAND_RE.test(text.trim())

    // A clarify parked mid-turn is one specific case of "agent blocked inside a
    // tool batch": the clarify tool call is itself the blocked tool. Releasing
    // it (clarify.respond) wakes that tool's worker thread, which can finish
    // the batch and drain any pending steer/redirect correction BEFORE this
    // steer's own RPC has round-tripped and staged that correction server-side
    // (agent.redirect() degrades to agent.steer() while a tool is executing,
    // per interrupt_control.py). Losing that race silently drops the user's
    // correction — the turn keeps running on its own, unedited, with no visual
    // sign anything went wrong until it finishes or the user hits Stop. So when
    // we're about to steer, release the clarify only AFTER steerDraft's RPC
    // settles (see steerDraft below); every other path (idle submit, slash
    // exec, queue) has no such race and keeps releasing it immediately.
    if (payloadPresent && clarifyPending && !willSteerClarify) {
      void skipClarifyRequest(sessionId)
    }

    if (queueEdit) {
      exitQueuedEdit('save')
    } else if (busy) {
      // Slash commands should execute immediately even while the agent is
      // busy — they're client-side operations (/yolo, /skin, /new, /help,
      // etc.) or self-contained gateway RPCs (/status, /compress).  onSubmit
      // routes them to executeSlashCommand, which has its own per-command
      // busy guard for commands that genuinely need an idle session (skill
      // /send directives).  Queuing them would make every slash command wait
      // for the current turn to finish, which is how the TUI never behaves.
      if (!attachments.length && SLASH_COMMAND_RE.test(text.trim())) {
        triggerHaptic('submit')
        clearDraft()
        dispatchSubmit(text)
      } else if (!compacting && !blockingPrompt && !attachments.length && text.trim()) {
        // Cursor-style stop-and-correct: interrupt the live turn and redirect
        // it with this text. redirect() preserves the shown reasoning/work; if
        // the turn already ended, steerDraft re-queues so nothing is lost.
        steerDraft(clarifyPending ? sessionId : null)
      } else if (payloadPresent) {
        // Attachments can't ride a redirect (no tool-result image carriage) —
        // queue the whole payload for the next turn. Same for a turn parked on
        // an approval/sudo/secret prompt: a steer can't reach the model while
        // the tool batch is blocked, so the message runs as the next turn.
        queueCurrentDraft()
      } else {
        // Stop button (the only way to reach here while busy with an empty
        // composer — empty Enter is short-circuited in the keydown handler).
        triggerHaptic('cancel')
        void Promise.resolve(onCancel())
      }
    } else if (!payloadPresent && queuedPrompts.length > 0) {
      void drainNextQueued()
    } else if (payloadPresent) {
      const submittedAttachments = cloneAttachments(attachments)
      triggerHaptic('submit')
      resetBrowseState(sessionId)
      clearDraft()
      scope.attachments.clear()
      dispatchSubmit(text, submittedAttachments)
    }

    focusInput()
  }

  // Redirect the live turn with a correction. The gateway either restarts the
  // active model request with its displayed context or waits for the current
  // tool boundary. If the turn already ended, queue the words instead.
  //
  // `releaseClarifyAfter`: when this correction is answering a clarify card by
  // typing past it (see submitDraft above), the clarify must stay parked on
  // the server until onSteer's RPC settles. Releasing it first (or in
  // parallel) lets the clarify tool's worker thread wake, finish its batch,
  // and drain any pending correction before this RPC ever lands — the
  // correction silently vanishes and the turn runs on unedited.
  const steerDraft = (releaseClarifyAfter?: string | null) => {
    const text = draftRef.current.trim()

    // Guard on live editor state, not the render-lagged `canSteer`: a redirect
    // fired on a fast Enter must not be dropped because state hasn't synced.
    if (!onSteer || !text || attachments.length > 0 || SLASH_COMMAND_RE.test(text)) {
      return
    }

    triggerHaptic('submit')
    clearDraft()

    void Promise.resolve(onSteer(text))
      .then(accepted => {
        if (!accepted && activeQueueSessionKey) {
          enqueueQueuedPrompt(activeQueueSessionKey, { text, attachments: [] })
        }

        return accepted
      })
      .finally(() => {
        // Only now is the correction guaranteed staged (accepted) or
        // definitely never going to land (rejected/thrown) — either way it's
        // safe to let the clarify's tool call return.
        if (releaseClarifyAfter) {
          void skipClarifyRequest(releaseClarifyAfter)
        }
      })
  }

  const queueDraft = () => {
    if (disabled || !busy) {
      return
    }

    queueCurrentDraft()
    focusInput()
  }

  return { dispatchSubmit, queueDraft, steerDraft, submitDraft }
}
