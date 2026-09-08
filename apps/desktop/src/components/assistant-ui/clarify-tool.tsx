'use client'

import { type ToolCallMessagePartProps, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import {
  type ComponentProps,
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react'

import { requestComposerFocus, requestComposerInsert } from '@/app/chat/composer/focus'
import { useSessionView } from '@/app/chat/session-view'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { WIDGET_SHELL_CLASS } from '@/components/chat/widget-shell'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Textarea } from '@/components/ui/textarea'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { ClarifyMarkdown, renderClarifyInline } from '@/lib/clarify-markdown'
import { triggerHaptic } from '@/lib/haptics'
import { CircleLetterA, Loader2, MessageQuestion } from '@/lib/icons'
import { visibleClarifyCard } from '@/lib/keybinds/composer-focus-keys'
import { cn } from '@/lib/utils'
import {
  $clarifyToolRequestIds,
  $settledClarifyHelp,
  associateClarifyToolRequest,
  bareChoice,
  type ClarifyHelp,
  type ClarifyQuestion,
  type ClarifyRequest,
  clearClarifyRequest,
  normalizeChoices,
  RECOMMENDED_LABEL,
  reconcileClarifyHelp,
  sessionClarifyRequest,
  updateClarifyHelp,
  warnDroppedChoices
} from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { requestForOwnedSession } from '@/store/session-states'

import { selectMessageRunning } from './tool/fallback-model'
import { parseMaybeObject } from './tool/fallback-model/format'

interface ClarifyArgs {
  question?: string
  choices?: string[] | null
  multiSelect?: boolean
  questions?: { question: string; choices?: string[] | null; multiSelect?: boolean }[]
}

interface ClarifyResult {
  question?: string
  answer?: string
  note?: string
  error?: string
}

// Distinct even when multiple help controls are activated in one event-loop turn.
let clarifyHelpLocalSequence = 0

function clarifyHelpErrorMessage(cause: unknown): string {
  const message = cause instanceof Error ? cause.message : ''
  const normalized = message.toLowerCase()

  if (
    /session (?:not found|expired|gone)|clarification (?:not found|expired|gone)|owner.*(?:unknown|unavailable)/.test(
      normalized
    )
  ) {
    return 'This clarification is no longer available. Return to the conversation and try again.'
  }

  if (/offline|connection (?:closed|lost|unavailable)|network/.test(normalized)) {
    return 'Help is temporarily unavailable. Check your connection and try again.'
  }

  return message || 'Help request failed. Try again.'
}

function stringField(row: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = row[key]

    if (typeof value === 'string') {
      return value
    }
  }
}

function readClarifyArgs(args: unknown): ClarifyArgs {
  const row = parseMaybeObject(args)
  const rawChoices = row.choices
  const choices = normalizeChoices(rawChoices)

  const question = stringField(row, 'question')

  if (rawChoices != null && choices.length === 0 && question) {
    warnDroppedChoices('tool_args', question, rawChoices)
  }

  // Batch form: tool args carry the model's questions array. Entries are
  // normalized leniently here (qid comes from the gateway request, not args).
  let questions: ClarifyArgs['questions']

  if (Array.isArray(row.questions)) {
    const parsed = row.questions
      .map(entry => {
        const item = parseMaybeObject(entry)
        const text = stringField(item, 'question')

        if (!text) {
          return null
        }

        const itemChoices = normalizeChoices(item.choices)

        return {
          choices: itemChoices.length > 0 ? itemChoices : null,
          multiSelect: item.multi_select === true && itemChoices.length > 0,
          question: text
        }
      })
      .filter((entry): entry is NonNullable<typeof entry> => entry !== null)

    if (parsed.length > 0) {
      questions = parsed
    }
  }

  return {
    question,
    choices: choices.length > 0 ? choices : null,
    multiSelect: row.multi_select === true,
    questions
  }
}

interface ClarifyBatchResponse {
  id?: string
  question?: string
  answer?: string | string[]
  note?: string
}

/** Parse batch clarify tool JSON (`responses` array + optional timed_out). */
export function readClarifyBatchResult(result: unknown): {
  responses: ClarifyBatchResponse[]
  timedOut: boolean
} {
  const row = parseMaybeObject(result)

  if (!Array.isArray(row.responses)) {
    return { responses: [], timedOut: false }
  }

  const responses = row.responses.map((entry): ClarifyBatchResponse => {
    const item = parseMaybeObject(entry)
    const answer = item.user_response

    return {
      answer: Array.isArray(answer) ? answer.map(String) : typeof answer === 'string' ? answer : undefined,
      id: stringField(item, 'id'),
      note: stringField(item, 'note'),
      question: stringField(item, 'question')
    }
  })

  return { responses, timedOut: row.timed_out === true }
}

/** Parse clarify tool JSON (`question` + `user_response`). */
export function readClarifyResult(result: unknown): ClarifyResult {
  const row = parseMaybeObject(result)

  if (Object.keys(row).length === 0) {
    return typeof result === 'string' && result.trim() ? { answer: result.trim() } : {}
  }

  return {
    question: stringField(row, 'question'),
    answer: stringField(row, 'user_response', 'answer'),
    note: stringField(row, 'note'),
    error: stringField(row, 'error')
  }
}

const letterFor = (index: number): string => String.fromCharCode(65 + index)

// The backend tags the agent's preferred option (`mark_recommended`); the card
// renders the label in tertiary text so the option itself still reads first.
// Choices are validated newline-free (normalizeChoices), so inline rendering
// (bold/code, no block/list splitting) is all a choice ever needs.
function ChoiceLabel({ choice }: { choice: string }) {
  const bare = bareChoice(choice)

  if (bare === choice) {
    return <>{renderClarifyInline(choice)}</>
  }

  return (
    <>
      {renderClarifyInline(bare)} <span className="text-(--ui-text-tertiary)">{RECOMMENDED_LABEL}</span>
    </>
  )
}

const OPTION_ROW_CLASS =
  'flex w-full items-start gap-2 rounded-[0.25rem] px-1.5 py-1 text-left disabled:cursor-not-allowed disabled:opacity-50'

// field-sizing on top of Textarea's shared chrome; kill min-h-16 for one-liners.
const CLARIFY_TEXTAREA_CLASS =
  'field-sizing-content max-h-40 min-h-0 resize-none placeholder:text-(--ui-text-secondary)'

const CLARIFY_SHELL_CLASS = `${WIDGET_SHELL_CLASS} text-[length:var(--conversation-text-font-size)] text-(--ui-text-primary)`

const CLARIFY_ICON_CLASS = 'mt-px size-4 shrink-0 text-(--ui-text-tertiary)'

function ClarifyShell({ children, className, ...props }: ComponentProps<'div'>) {
  return (
    <div className={cn(CLARIFY_SHELL_CLASS, className)} data-slot="clarify-inline" {...props}>
      {children}
    </div>
  )
}

function ClarifyLine({
  children,
  className,
  icon: Icon,
  ...props
}: ComponentProps<'div'> & { icon: typeof MessageQuestion }) {
  return (
    <div className={cn('flex items-start gap-2', className)} {...props}>
      <div className="min-w-0 flex-1">{children}</div>
      <Icon aria-hidden className={CLARIFY_ICON_CLASS} />
    </div>
  )
}

function KeyBadge({ char, preview, selected }: { char: string; preview?: boolean; selected: boolean }) {
  return (
    <Kbd
      className={cn(
        'mt-px',
        selected && 'border-primary bg-primary text-white shadow-none',
        !selected && preview && 'border-primary text-primary shadow-none'
      )}
      size="sm"
    >
      {char}
    </Kbd>
  )
}

function ClarifyHelpControls({
  choice,
  questionId,
  request,
  target,
  targetLabel
}: {
  choice?: string
  questionId?: string
  request: ClarifyRequest | null
  target: string
  targetLabel: string
}) {
  const gateway = useStore($gateway)
  const [askOpen, setAskOpen] = useState(false)
  const [followUp, setFollowUp] = useState('')
  const [error, setError] = useState('')

  const help = useMemo(
    () => Object.values(request?.help ?? {}).filter(item => item.questionId === questionId && item.choice === choice),
    [choice, questionId, request?.help]
  )

  const requestHelp = useCallback(
    async (custom = '') => {
      if (!request || !gateway) {
        setError('Help is unavailable while the connection is offline.')

        return
      }

      const localId = `local-${++clarifyHelpLocalSequence}`
      setError('')
      updateClarifyHelp(request.requestId, request.sessionId, localId, {
        choice,
        followUp: custom,
        questionId,
        status: 'loading'
      })

      try {
        const response = await requestForOwnedSession(
          request.sessionId,
          gateway.request.bind(gateway) as typeof gateway.request,
          'clarify.explain',
          {
            ...(choice ? { choice: bareChoice(choice) } : {}),
            ...(custom ? { follow_up: custom } : {}),
            ...(questionId ? { question_id: questionId } : {}),
            request_id: request.requestId,
            // Owner routing selects the right gateway connection, but the
            // clarify RPC itself resolves its pending request by runtime id.
            // Without this wire field `_sess()` sees an empty session and
            // rejects the otherwise-correct owner request as "session not found".
            session_id: request.sessionId,
            version: 1
          }
        )

        const explanationId =
          typeof response === 'object' &&
          response !== null &&
          typeof (response as Record<string, unknown>).explanation_id === 'string'
            ? (response as Record<string, string>).explanation_id
            : ''

        if (explanationId) {
          reconcileClarifyHelp(request.requestId, request.sessionId, localId, explanationId)
        }
      } catch (cause) {
        updateClarifyHelp(request.requestId, request.sessionId, localId, {
          choice,
          error: clarifyHelpErrorMessage(cause),
          followUp: custom,
          questionId,
          status: 'error'
        })
      }
    },
    [choice, gateway, questionId, request]
  )

  return (
    <div className="grid min-w-0 gap-1" data-clarify-help-target={target}>
      <div className="flex items-center gap-1">
        <Button
          aria-label={`Why? ${targetLabel}`}
          onClick={() => void requestHelp()}
          size="xs"
          type="button"
          variant="text"
        >
          Why?
        </Button>
        <Button
          aria-controls={`clarify-help-${target}`}
          aria-expanded={askOpen}
          aria-label={`Ask about ${targetLabel}`}
          onClick={() => setAskOpen(open => !open)}
          size="xs"
          type="button"
          variant="text"
        >
          Ask
        </Button>
      </div>
      {askOpen || help.length > 0 || error ? (
        <div className="grid gap-1 border-l border-(--ui-stroke-tertiary) pl-2 text-sm" id={`clarify-help-${target}`}>
          {help.map(item =>
            item.status === 'loading' ? (
              <span key={item.explanationId} role="status">
                Getting help…
              </span>
            ) : item.content ? (
              <ClarifyMarkdown key={item.explanationId} text={item.content} />
            ) : item.error ? (
              <p className="text-destructive" key={item.explanationId}>
                {item.error}
              </p>
            ) : null
          )}
          {error ? <p className="text-destructive">{error}</p> : null}
          {askOpen ? (
            <div className="grid gap-1" data-clarify-follow-up={target}>
              <Textarea
                aria-label={`Follow-up for ${target}`}
                className="min-h-0 placeholder:text-(--ui-text-secondary)"
                onChange={event => setFollowUp(event.target.value)}
                placeholder="Ask a follow-up…"
                rows={1}
                size="sm"
                value={followUp}
              />
              <div className="flex justify-end">
                <Button
                  disabled={!followUp.trim()}
                  onClick={() => void requestHelp(followUp.trim())}
                  size="xs"
                  type="button"
                >
                  Send
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function ClarifyNoteControl({
  disabled,
  label,
  note,
  onChange,
  onKeyDown,
  onOpen,
  open
}: {
  disabled: boolean
  label: string
  note: string
  onChange: (value: string) => void
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  onOpen: () => void
  open: boolean
}) {
  const { t } = useI18n()
  const copy = t.assistant.clarify

  return (
    <div className="ml-7 grid gap-1 border-l border-(--ui-stroke-tertiary) pl-2" data-clarify-note={label}>
      {open ? (
        <Textarea
          aria-label={copy.noteFor(label)}
          className={CLARIFY_TEXTAREA_CLASS}
          disabled={disabled}
          onChange={event => onChange(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={copy.notePlaceholder}
          rows={1}
          size="sm"
          value={note}
        />
      ) : (
        <Button
          aria-label={copy.addNoteFor(label)}
          disabled={disabled}
          onClick={onOpen}
          size="xs"
          type="button"
          variant="text"
        >
          {copy.addNote}
        </Button>
      )}
    </div>
  )
}

/** A letter-badged option row. Shared by the live pending card (where a click
 * selects an answer) and the settled skip card (where a click drafts a
 * follow-up), so both stay visually identical. */
function ChoiceButton({
  active = false,
  char,
  choice,
  disabled,
  keyShortcuts,
  onClick,
  selected,
  title
}: {
  active?: boolean
  char: string
  choice: string
  disabled?: boolean
  keyShortcuts?: string
  onClick: () => void
  selected?: boolean
  title?: string
}) {
  // `Tip` is the repo's themed replacement for native `title=` (a native
  // tooltip on a <button> is banned by the no-native-title guard). It renders
  // the child untouched when `label` is falsy, so the live card (no tip) is
  // unaffected and only the settled skip card gets the hover hint.
  //
  // `active` is the keyboard cursor on the live card (arrow-key navigation);
  // it highlights the row and previews its key badge. The settled skip card
  // never passes it, so its rows stay plain.
  return (
    <Tip label={title}>
      <button
        aria-current={active || undefined}
        aria-keyshortcuts={keyShortcuts}
        aria-pressed={selected}
        className={cn(
          OPTION_ROW_CLASS,
          'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
          active && 'bg-(--chrome-action-hover) text-(--ui-text-primary)',
          selected && 'text-(--ui-text-primary)'
        )}
        data-choice
        data-highlighted={active || undefined}
        disabled={disabled}
        onClick={onClick}
        type="button"
      >
        <KeyBadge char={char} preview={active} selected={Boolean(selected)} />
        <span className="flex-1 wrap-anywhere">
          <ChoiceLabel choice={choice} />
        </span>
      </button>
    </Tip>
  )
}

export const ClarifyTool = (props: ToolCallMessagePartProps) => {
  // Keep the request association through the brief clear → tool.complete gap so
  // in-place help can remain on the settled card without a transcript message.
  const sessionId = useStore(useSessionView().$runtimeId)
  const request = useStore(useMemo(() => sessionClarifyRequest(sessionId), [sessionId]))
  const toolRequestIds = useStore($clarifyToolRequestIds)
  const settledHelp = useStore($settledClarifyHelp)
  const lastRequest = useRef<ClarifyRequest | null>(null)

  if (request) {
    lastRequest.current = request
  }

  useEffect(() => {
    if (request) {
      associateClarifyToolRequest(props.toolCallId, request.requestId)
    }
  }, [props.toolCallId, request])

  if (props.result !== undefined) {
    const requestId = toolRequestIds[props.toolCallId]
    const help = lastRequest.current?.help ?? (requestId ? (settledHelp[requestId] ?? {}) : {})

    return <ClarifyToolSettled {...props} help={help} />
  }

  return <ClarifyToolPending {...props} />
}

function ClarifyHelpDetails({ help }: { help: Record<string, ClarifyHelp> }) {
  const entries = Object.values(help).filter(item => item.content)

  if (entries.length === 0) {
    return null
  }

  return (
    <details className="text-sm text-(--ui-text-secondary)">
      <summary>Help requested</summary>
      <div className="mt-1 grid gap-1">
        {entries.map(item => (
          <ClarifyMarkdown key={item.explanationId} text={item.content ?? ''} />
        ))}
      </div>
    </details>
  )
}

function ClarifyToolSettled({ help, ...props }: ToolCallMessagePartProps & { help: Record<string, ClarifyHelp> }) {
  const batch = readClarifyBatchResult(props.result)

  if (batch.responses.length > 0) {
    return <ClarifyToolBatchSettled help={help} responses={batch.responses} />
  }

  return <ClarifyToolSingleSettled {...props} help={help} />
}

function ClarifyToolSingleSettled({
  args,
  help,
  result
}: ToolCallMessagePartProps & { help: Record<string, ClarifyHelp> }) {
  const { t } = useI18n()
  const copy = t.assistant.clarify
  const fromArgs = useMemo(() => readClarifyArgs(args), [args])
  const fromResult = useMemo(() => readClarifyResult(result), [result])

  const question = fromResult.question || fromArgs.question || ''
  const answer = fromResult.answer
  const note = fromResult.note
  const error = fromResult.error
  const skipped = !error && answer !== undefined && !answer.trim()
  const answerText = error || (skipped ? copy.skipped : (answer ?? '').trim())
  const choices = fromArgs.choices ?? []

  // A skipped (timed-out) clarify keeps its choices on screen and actionable.
  // The blocking request is long gone — the tool already returned empty — so a
  // pick can't resolve it retroactively. Instead it drafts a quoted follow-up
  // into the composer (Enter sends; if the agent is mid-turn it queues like
  // any other prompt). Without this the card collapsed to just "Skipped" and
  // the options were unrecoverable.
  const followUp = useCallback(
    (choice: string) => {
      requestComposerInsert(copy.lateAnswer(question, choice), { mode: 'block' })
      requestComposerFocus()
      triggerHaptic('selection')
    },
    [copy, question]
  )

  return (
    <ClarifyShell className="my-1.5 grid gap-1.5" data-clarify-settled="">
      {question ? (
        <ClarifyLine icon={MessageQuestion}>
          <div className="font-medium leading-(--conversation-line-height)">
            <ClarifyMarkdown text={question} />
          </div>
        </ClarifyLine>
      ) : null}
      {answerText ? (
        <ClarifyLine icon={CircleLetterA}>
          <div className="grid gap-0.5">
            <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{copy.selected}</span>
            <div
              className={cn(
                'leading-(--conversation-line-height)',
                error ? 'text-destructive' : 'text-(--ui-text-secondary)',
                skipped && 'italic text-(--ui-text-tertiary)'
              )}
              data-clarify-answer=""
            >
              <ClarifyMarkdown text={answerText} />
            </div>
          </div>
        </ClarifyLine>
      ) : null}
      {note?.trim() ? (
        <ClarifyLine icon={CircleLetterA}>
          <div className="grid gap-0.5">
            <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{copy.note}</span>
            <div className="leading-(--conversation-line-height) text-(--ui-text-secondary)">
              <ClarifyMarkdown text={note} />
            </div>
          </div>
        </ClarifyLine>
      ) : null}
      {skipped && choices.length > 0 ? (
        <div className="grid gap-px" data-clarify-late-choices="" role="group">
          {choices.map((choice, index) => (
            <ChoiceButton
              char={letterFor(index)}
              choice={choice}
              key={`${index}-${choice}`}
              onClick={() => followUp(choice)}
              title={copy.lateAnswerTip}
            />
          ))}
          <p className="px-1.5 pt-0.5 text-[0.6875rem] leading-4 text-(--ui-text-tertiary)">{copy.lateAnswerHint}</p>
        </div>
      ) : null}
      <ClarifyHelpDetails help={help} />
    </ClarifyShell>
  )
}

function ClarifyToolPending(props: ToolCallMessagePartProps) {
  // The tool row is in whichever session's transcript rendered it — read THAT
  // session's clarify (primary or tile), not the globally-active one.
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionClarifyRequest(sessionId), [sessionId])
  const request = useStore($request)
  const fromArgs = useMemo(() => readClarifyArgs(props.args), [props.args])
  const messageRunning = useAuiState(selectMessageRunning)
  // Answering clears the request a beat before `tool.complete` swaps in the
  // settled card. Latch submit so that gap doesn't demote; Stop also clears
  // the request and must still collapse an unanswered card.
  const [answered, setAnswered] = useState(false)

  // Stopped mid-prompt with no result — don't leave a dead interactive panel.
  // `session.info` reports running=false while clarify is blocking, so the
  // running flag alone would remount the question as a tool row. Keep the
  // card while a request is open or this instance already submitted.
  if (!messageRunning && !request && !answered) {
    return <ToolFallback {...props} />
  }

  // Batch: the gateway request carries qid-keyed questions. Args alone can't
  // drive the form (no qids to respond with), so batch waits for the request.
  if (request?.questions?.length || fromArgs.questions) {
    return <ClarifyToolBatchPending onAnswered={() => setAnswered(true)} request={request} />
  }

  return <ClarifyToolSinglePending fromArgs={fromArgs} onAnswered={() => setAnswered(true)} request={request} />
}

function ClarifyToolSinglePending({
  fromArgs,
  onAnswered,
  request
}: {
  fromArgs: ClarifyArgs
  onAnswered: () => void
  request: ClarifyRequest | null
}) {
  const { t } = useI18n()
  const copy = t.assistant.clarify
  const gateway = useStore($gateway)

  const matchingRequest = useMemo(() => {
    if (!request || request.questions?.length) {
      return null
    }

    if (fromArgs.question && request.question && fromArgs.question !== request.question) {
      return null
    }

    return request
  }, [fromArgs.question, request])

  const question = fromArgs.question || matchingRequest?.question || ''

  const choices = useMemo(
    // Prefer the gateway request's choices over the raw tool args: the backend
    // labels the recommended option there (`mark_recommended`), and the card
    // only renders once `matchingRequest` exists, so the args are a fallback
    // for a hydration race, not the normal path.
    () => matchingRequest?.choices ?? fromArgs.choices ?? [],
    [fromArgs.choices, matchingRequest?.choices]
  )

  const hasChoices = choices.length > 0
  const multiSelect = hasChoices && Boolean(matchingRequest?.multiSelect ?? fromArgs.multiSelect)

  const [draft, setDraft] = useState('')
  const [note, setNote] = useState('')
  const [noteAnchor, setNoteAnchor] = useState<string | null>(null)
  const [noteOpen, setNoteOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const [selectedChoices, setSelectedChoices] = useState<string[]>([])
  // The keyboard cursor. Indices 0..choices.length-1 are the options; the
  // trailing index (=== choices.length) is the "Other" free-text row.
  const [activeIndex, setActiveIndex] = useState(0)
  const [otherFocused, setOtherFocused] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  // Identity for the visible-card check below — this is the element carrying
  // `data-clarify-choices`, i.e. the one `visibleClarifyCard()` resolves.
  const formRef = useRef<HTMLFormElement | null>(null)

  // Race: tool.start fires a tick before clarify.request, so request_id
  // arrives slightly after the tool block mounts. If the question text is
  // already in the tool args, paint the card immediately (disabled until
  // the request is wired) — a spinner→question swap is a layout jump for
  // no reason. Only spin when we have nothing to show yet.
  const ready = Boolean(matchingRequest?.requestId)
  const loading = !ready && !submitting && !question

  const respond = useCallback(
    async (answer: string, responseNote?: string) => {
      if (submittingRef.current) {
        return
      }

      if (!ready || !matchingRequest) {
        notifyError(new Error(copy.notReady), copy.sendFailed)

        return
      }

      if (!gateway) {
        notifyError(new Error(copy.gatewayDisconnected), copy.sendFailed)

        return
      }

      submittingRef.current = true
      setSubmitting(true)

      try {
        // Route through the session's OWNER (tile route → hint → tagged row);
        // legacy ambient is allowed only when it is provably the sole backend.
        // The ambient socket follows foreground focus, so after a profile / Bot
        // Chat switch it can point at a backend that never held this clarify —
        // and the owner stays blocked (#91684 client half, like approval.respond).
        await requestForOwnedSession<{ ok?: boolean }>(
          matchingRequest.sessionId,
          // Bound (not wrapped) so the ambient fallback keeps the exact 2-arg
          // call shape gateway.request callers assert on.
          gateway.request.bind(gateway) as typeof gateway.request,
          'clarify.respond',
          {
            answer,
            ...(responseNote?.trim() ? { note: responseNote.trim() } : {}),
            request_id: matchingRequest.requestId
          }
        )
        triggerHaptic('submit')
        onAnswered()
        clearClarifyRequest(matchingRequest.requestId, matchingRequest.sessionId)
        // tool.complete lands next → ClarifyToolSettled.
      } catch (error) {
        notifyError(error, copy.sendFailed)
        submittingRef.current = false
        setSubmitting(false)
      }
    },
    [copy.gatewayDisconnected, copy.notReady, copy.sendFailed, gateway, matchingRequest, onAnswered, ready]
  )

  const trimmedDraft = draft.trim()
  // The answer is whichever input is active: a picked choice, or typed text.
  // Picking a choice no longer fires immediately — it selects, then the user
  // confirms with Continue (or Enter from the field).

  const selectedAnswer = multiSelect
    ? selectedChoices.length > 0
      ? JSON.stringify(selectedChoices)
      : null
    : (selectedChoices[0] ?? null)

  const pendingAnswer = selectedAnswer ?? (trimmedDraft || null)

  const selectChoice = useCallback(
    (choice: string, index: number) => {
      // Picking a choice and typing are mutually exclusive answers.
      setDraft('')
      setSelectedChoices(selected => {
        let next: string[]

        if (!multiSelect) {
          next = [choice]
        } else {
          next = selected.includes(choice) ? selected.filter(value => value !== choice) : [...selected, choice]
        }

        setNoteAnchor(anchor => {
          if (next.length === 0) {
            return null
          }

          if (anchor && next.includes(anchor)) {
            return anchor
          }

          return noteOpen ? next[0] : null
        })

        return next
      })
      setActiveIndex(index)
    },
    [multiSelect, noteOpen]
  )

  // Keep the cursor in range when the choice set changes (never past "Other").
  useEffect(() => {
    setActiveIndex(index => Math.min(index, choices.length))
  }, [choices.length])

  const moveActive = useCallback(
    (delta: number) => {
      const itemCount = choices.length + 1

      // Arrow navigation is a move, not a pick. Multi-select keeps staged
      // choices while the cursor moves so the user can build a set; the
      // single-select path retains its existing clear-on-navigation behaviour.
      setDraft('')

      if (!multiSelect) {
        setSelectedChoices([])
      }

      setActiveIndex(index => (index + delta + itemCount) % itemCount)
    },
    [choices.length, multiSelect]
  )

  const submitAnswer = useCallback(() => {
    if (pendingAnswer) {
      void respond(pendingAnswer, note)
    }
  }, [note, pendingAnswer, respond])

  const activateActive = useCallback(() => {
    const choice = choices[activeIndex]

    // Multi-select Enter toggles the highlighted choice. The user confirms the
    // staged set explicitly with Continue so this path never submits a scalar.
    if (multiSelect && choice) {
      selectChoice(choice, activeIndex)

      return
    }

    // A staged answer (picked choice or typed text) wins — confirm it.
    if (pendingAnswer) {
      submitAnswer()

      return
    }

    // Otherwise act on the highlighted row: a choice responds immediately, and
    // the trailing "Other" row focuses the free-text field.
    if (choice) {
      void respond(choice)

      return
    }

    textareaRef.current?.focus()
  }, [activeIndex, choices, multiSelect, pendingAnswer, respond, selectChoice, submitAnswer])

  const handleTextareaKey = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.nativeEvent.isComposing) {
        return
      }

      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        submitAnswer()
      }
    },
    [submitAnswer]
  )

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      submitAnswer()
    },
    [submitAnswer]
  )

  // Arrow keys move a visual cursor, 1-9 and A/B/C… pick directly, and Enter
  // confirms the current answer (or acts on the highlighted row). Stands down
  // whenever a focusable control (a field, a choice button, the action bar) is
  // focused, so it never eats keystrokes meant for the composer, the Other box,
  // or a button the user tabbed to — and whenever this card is not the visible
  // one, since the binding is window-wide but the answer is session-specific.
  useEffect(() => {
    if (!ready || !hasChoices || submitting) {
      return
    }

    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) {
        return
      }

      // Not the visible card ⇒ not our keystroke. Inactive tabs stay MOUNTED,
      // so every parked clarify keeps a live `window` listener; without this the
      // card that acts is whichever mounted first, and answering the question in
      // front of you silently answers a background session's question instead —
      // resuming an agent turn the user never saw. Same resolver the composer's
      // `clarifyCardOwnsKey` yields to, so the two cannot disagree about which
      // card is live.
      if (visibleClarifyCard() !== formRef.current) {
        return
      }

      const active = document.activeElement as HTMLElement | null

      if (
        active &&
        (active.isContentEditable || active.matches('a[href], button, input, select, textarea, [role="button"]'))
      ) {
        return
      }

      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        moveActive(event.key === 'ArrowDown' ? 1 : -1)

        return
      }

      if (/^[1-9]$/.test(event.key)) {
        const index = Number(event.key) - 1

        if (index < choices.length) {
          event.preventDefault()
          selectChoice(choices[index], index)
        } else if (index === choices.length) {
          event.preventDefault()
          setActiveIndex(index)
          textareaRef.current?.focus()
        }

        return
      }

      const key = event.key.toLowerCase()

      // Only the letters this card actually renders a row for. Anything past
      // the last row belongs to the composer — the user is typing a message
      // instead of picking an option, and swallowing the keystroke here would
      // make the first letter of it vanish.
      if (key.length === 1 && key >= 'a' && key <= 'z') {
        const index = key.charCodeAt(0) - 97

        if (index < choices.length) {
          event.preventDefault()
          selectChoice(choices[index], index)
        } else if (index === choices.length) {
          event.preventDefault()
          setActiveIndex(index)
          textareaRef.current?.focus()
        }

        return
      }

      if (event.key === 'Enter') {
        event.preventDefault()
        activateActive()
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [activateActive, choices, hasChoices, moveActive, ready, selectChoice, submitting])

  if (loading) {
    return (
      <ClarifyShell aria-label={copy.loadingQuestion} className="my-1.5 grid min-h-12 place-items-center" role="status">
        <Loader2 aria-hidden className="size-4 animate-spin text-(--ui-text-tertiary)" />
      </ClarifyShell>
    )
  }

  const onDraftChange = (value: string) => {
    setDraft(value)

    // Typing is its own answer — drop any picked choice so the two inputs can't
    // both look selected.
    if (value.trim()) {
      setSelectedChoices([])
      setNoteAnchor(null)
    }
  }

  return (
    // `data-clarify-choices` marks the panel as owning its OWN shortcut keys
    // (Enter, and 1..N+1 / A.. for the N choices plus "Other") while they're
    // live, so the global type-to-focus listener (`clarifyCardOwnsKey`) yields
    // exactly those and lets every other printable through to the composer —
    // typing a real message instead of picking an option stays possible. The
    // value is the choice count so the check needs no store access.
    //
    // The form is the outer element so the actions can sit OUTSIDE the card and
    // still submit it — the panel holds the question, the buttons ride below it.
    <form
      className="my-1.5 grid gap-4"
      data-clarify-choices={hasChoices ? choices.length : undefined}
      onSubmit={handleSubmit}
      ref={formRef}
    >
      <ClarifyShell className="grid gap-2">
        <div className="flex items-start gap-2">
          <div className="flex-1 font-medium leading-(--conversation-line-height)">
            <ClarifyMarkdown text={question} />
          </div>
          <MessageQuestion aria-hidden className="mt-px size-4 shrink-0 text-(--ui-text-tertiary)" />
        </div>
        <ClarifyHelpControls request={matchingRequest} target="question" targetLabel={question} />

        {hasChoices ? (
          <div className="grid gap-px" role="group">
            {choices.map((choice, index) => {
              const selected = selectedChoices.includes(choice)

              return (
                <div className="grid gap-1" key={`${index}-${choice}`}>
                  <ChoiceButton
                    active={activeIndex === index}
                    char={letterFor(index)}
                    choice={choice}
                    disabled={submitting || !ready}
                    keyShortcuts={`${letterFor(index)} ${index + 1}`}
                    onClick={() => selectChoice(choice, index)}
                    selected={selected}
                  />
                  <ClarifyHelpControls
                    choice={bareChoice(choice)}
                    request={matchingRequest}
                    target={`choice-${index}`}
                    targetLabel={bareChoice(choice)}
                  />
                  {selected && (!noteOpen || noteAnchor === choice) ? (
                    <ClarifyNoteControl
                      disabled={submitting || !ready}
                      label={bareChoice(choice)}
                      note={note}
                      onChange={setNote}
                      onKeyDown={handleTextareaKey}
                      onOpen={() => {
                        setNoteAnchor(choice)
                        setNoteOpen(true)
                      }}
                      open={noteOpen && noteAnchor === choice}
                    />
                  ) : null}
                </div>
              )
            })}
            <label
              className={cn(
                OPTION_ROW_CLASS,
                'items-center',
                activeIndex === choices.length && 'bg-(--chrome-action-hover)'
              )}
              data-highlighted={activeIndex === choices.length || undefined}
            >
              <KeyBadge
                char={letterFor(choices.length)}
                preview={otherFocused || activeIndex === choices.length}
                selected={Boolean(trimmedDraft)}
              />
              <Textarea
                aria-current={activeIndex === choices.length || undefined}
                aria-keyshortcuts={`${letterFor(choices.length)} ${choices.length + 1}`}
                className={CLARIFY_TEXTAREA_CLASS}
                disabled={submitting || !ready}
                onBlur={() => setOtherFocused(false)}
                onChange={event => onDraftChange(event.target.value)}
                onFocus={() => {
                  setSelectedChoices([])
                  setActiveIndex(choices.length)
                  setOtherFocused(true)
                }}
                onKeyDown={handleTextareaKey}
                placeholder={copy.other}
                ref={textareaRef}
                rows={1}
                size="sm"
                value={draft}
              />
            </label>
            {trimmedDraft ? (
              <ClarifyNoteControl
                disabled={submitting || !ready}
                label={copy.other}
                note={note}
                onChange={setNote}
                onKeyDown={handleTextareaKey}
                onOpen={() => {
                  setNoteAnchor(null)
                  setNoteOpen(true)
                }}
                open={noteOpen}
              />
            ) : null}
          </div>
        ) : (
          <Textarea
            className={CLARIFY_TEXTAREA_CLASS}
            disabled={submitting || !ready}
            onChange={event => onDraftChange(event.target.value)}
            onKeyDown={handleTextareaKey}
            placeholder={copy.placeholder}
            ref={textareaRef}
            rows={1}
            size="sm"
            value={draft}
          />
        )}
      </ClarifyShell>

      <div className="flex items-center justify-end gap-1">
        <Button disabled={submitting || !ready} onClick={() => void respond('')} size="xs" type="button" variant="text">
          {copy.skip}
        </Button>
        <Button disabled={submitting || !ready || !pendingAnswer} size="xs" type="submit">
          {submitting ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <>
              {copy.continueLabel}
              <span aria-hidden className="ml-0.5 text-[0.625rem] opacity-70">
                ⏎
              </span>
            </>
          )}
        </Button>
      </div>
    </form>
  )
}

// ─── Batch (multi-question) clarify ─────────────────────────────────────────

/** Settled batch card: every question with its locked (or absent) answer. */
function ClarifyToolBatchSettled({
  help,
  responses
}: {
  help: Record<string, ClarifyHelp>
  responses: { question?: string; answer?: string | string[]; note?: string }[]
}) {
  const { t } = useI18n()
  const copy = t.assistant.clarify

  return (
    <ClarifyShell className="my-1.5 grid gap-2.5" data-clarify-settled="">
      {responses.map((row, index) => {
        const answer = Array.isArray(row.answer) ? row.answer.join(', ') : (row.answer ?? '')
        const blank = !answer.trim()

        return (
          <div className="grid gap-1" key={`${index}-${row.question ?? ''}`}>
            {row.question ? (
              <ClarifyLine icon={MessageQuestion}>
                <div className="font-medium leading-(--conversation-line-height)">
                  <ClarifyMarkdown text={row.question} />
                </div>
              </ClarifyLine>
            ) : null}
            <ClarifyLine icon={CircleLetterA}>
              <div className="grid gap-0.5">
                <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{copy.selected}</span>
                <div
                  className={cn(
                    'leading-(--conversation-line-height)',
                    blank ? 'italic text-(--ui-text-tertiary)' : 'text-(--ui-text-secondary)'
                  )}
                  data-clarify-answer=""
                >
                  {blank ? copy.skipped : <ClarifyMarkdown text={answer} />}
                </div>
              </div>
            </ClarifyLine>
            {row.note?.trim() ? (
              <ClarifyLine icon={CircleLetterA}>
                <div className="grid gap-0.5">
                  <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{copy.note}</span>
                  <div className="leading-(--conversation-line-height) text-(--ui-text-secondary)">
                    <ClarifyMarkdown text={row.note} />
                  </div>
                </div>
              </ClarifyLine>
            ) : null}
          </div>
        )
      })}
      <ClarifyHelpDetails help={help} />
    </ClarifyShell>
  )
}

/** One question's interactive block inside the live batch card.
 *
 * Each question is its own carded row rather than another run of bullets: a
 * numbered index chip, the prompt, and the options indented beneath it. With
 * up to `MAX_CHOICES` options per question and up to 5 questions, a flat
 * bulleted list gave the eye nothing to anchor on — the blocks were
 * indistinguishable and the form read as one long column of dashes. */
function BatchQuestionBlock({
  disabled,
  index,
  locked,
  onDraft,
  onFieldKeyDown,
  onNote,
  onNoteOpen,
  onToggle,
  question,
  request,
  staged,
  total
}: {
  disabled: boolean
  index: number
  locked: boolean
  onDraft: (value: string) => void
  onNote: (value: string) => void
  onNoteOpen: (choice: string | null) => void
  onToggle: (choice: string) => void
  onFieldKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  question: ClarifyQuestion
  request: ClarifyRequest | null
  staged: { choices: string[]; draft: string; note: string; noteAnchor: string | null; noteOpen: boolean }
  total: number
}) {
  const { t } = useI18n()
  const copy = t.assistant.clarify
  const choices = question.choices ?? []
  const answered = staged.choices.length > 0 || Boolean(staged.draft.trim())

  return (
    <div
      className={cn(
        'grid gap-2 rounded-2xl px-3 py-2.5 transition-colors',
        // The answered block recedes; the unanswered one keeps the accent rail,
        // so "what still needs me" is legible at a glance instead of requiring
        // the user to re-read every question.
        answered ? 'bg-transparent' : 'bg-(--chrome-action-hover)/40'
      )}
      data-clarify-answered={answered || undefined}
      data-clarify-batch-question={question.qid}
      data-locked={locked || undefined}
    >
      <div className="flex items-start gap-2">
        <span
          aria-hidden
          className={cn(
            'mt-px grid size-[1.125rem] shrink-0 place-items-center rounded-full text-[0.625rem] font-medium tabular-nums transition-colors',
            answered
              ? 'bg-primary text-white'
              : 'bg-(--chrome-action-hover) text-(--ui-text-tertiary) ring-1 ring-(--ui-text-tertiary)/20'
          )}
        >
          {answered ? '✓' : index + 1}
        </span>
        <div className="flex-1 font-medium leading-(--conversation-line-height)">
          <ClarifyMarkdown text={question.question} />
        </div>
        <span className="mt-0.5 shrink-0 text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">
          {index + 1}/{total}
        </span>
        {locked ? (
          <span className="shrink-0 rounded-sm bg-(--chrome-action-hover) px-1 py-px text-[0.625rem] text-(--ui-text-tertiary)">
            ✓ {copy.answeredBadge}
          </span>
        ) : null}
      </div>
      <ClarifyHelpControls
        questionId={question.qid}
        request={request}
        target={`question-${question.qid}`}
        targetLabel={question.question}
      />

      {choices.length > 0 ? (
        <div className="grid gap-px pl-[1.625rem]" role="group">
          {choices.map((choice, choiceIndex) => {
            const selected = staged.choices.includes(choice)

            return (
              <div className="grid gap-1" key={`${choiceIndex}-${choice}`}>
                <ChoiceButton
                  char={letterFor(choiceIndex)}
                  choice={choice}
                  disabled={disabled}
                  onClick={() => onToggle(choice)}
                  selected={selected}
                />
                <ClarifyHelpControls
                  choice={bareChoice(choice)}
                  questionId={question.qid}
                  request={request}
                  target={`choice-${question.qid}-${choiceIndex}`}
                  targetLabel={bareChoice(choice)}
                />
                {selected && (!staged.noteOpen || staged.noteAnchor === choice) ? (
                  <ClarifyNoteControl
                    disabled={disabled}
                    label={bareChoice(choice)}
                    note={staged.note}
                    onChange={onNote}
                    onKeyDown={onFieldKeyDown}
                    onOpen={() => onNoteOpen(choice)}
                    open={staged.noteOpen && staged.noteAnchor === choice}
                  />
                ) : null}
              </div>
            )
          })}
          <label className={cn(OPTION_ROW_CLASS, 'items-center')}>
            <KeyBadge char={letterFor(choices.length)} selected={Boolean(staged.draft.trim())} />
            <Textarea
              className={CLARIFY_TEXTAREA_CLASS}
              disabled={disabled}
              onChange={event => onDraft(event.target.value)}
              onKeyDown={onFieldKeyDown}
              placeholder={copy.other}
              rows={1}
              size="sm"
              value={staged.draft}
            />
          </label>
          {staged.draft.trim() ? (
            <ClarifyNoteControl
              disabled={disabled}
              label={copy.other}
              note={staged.note}
              onChange={onNote}
              onKeyDown={onFieldKeyDown}
              onOpen={() => onNoteOpen(null)}
              open={staged.noteOpen}
            />
          ) : null}
        </div>
      ) : (
        <Textarea
          className={CLARIFY_TEXTAREA_CLASS}
          disabled={disabled}
          onChange={event => onDraft(event.target.value)}
          onKeyDown={onFieldKeyDown}
          placeholder={copy.placeholder}
          rows={1}
          size="sm"
          value={staged.draft}
        />
      )}
    </div>
  )
}

const emptyStage = { choices: [] as string[], draft: '', note: '', noteAnchor: null as string | null, noteOpen: false }

/** Live batch card: all questions at once, staged locally, ONE confirm.
 * Picks and drafts stay in component state — nothing reaches the server
 * until every question has a staged answer and the user presses the single
 * "Confirm and continue" button, which sends the per-question locks
 * back-to-back and completes the batch. Staged answers stay editable up to
 * that moment. The per-question wire protocol is unchanged (the TUI/CLI
 * still lock incrementally); this card just batches its locks at the end. */
function ClarifyToolBatchPending({ onAnswered, request }: { onAnswered: () => void; request: ClarifyRequest | null }) {
  const { t } = useI18n()
  const copy = t.assistant.clarify
  const gateway = useStore($gateway)

  // qids only exist on the gateway request — args are a hydration-race
  // fallback for display, never answerable (no ids to respond with).
  const questions = useMemo(() => request?.questions ?? [], [request?.questions])
  const ready = Boolean(request?.requestId) && questions.length > 0

  const [staged, setStaged] = useState<
    Record<string, { choices: string[]; draft: string; note: string; noteAnchor: string | null; noteOpen: boolean }>
  >({})

  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const formRef = useRef<HTMLFormElement | null>(null)

  // Reconnect replay: answers the server already locked (an earlier window's
  // partial progress) pre-stage their questions so the restored card shows
  // them selected instead of blank.
  useEffect(() => {
    const lockedAnswers = request?.lockedAnswers
    const lockedNotes = request?.lockedNotes

    if (!lockedAnswers) {
      return
    }

    setStaged(current => {
      const next = { ...current }

      for (const question of questions) {
        const answer = lockedAnswers[question.qid]

        if (answer === undefined || next[question.qid]) {
          continue
        }

        const options = question.choices ?? []
        let replayedAnswers = [answer]

        if (question.multiSelect) {
          try {
            const parsed = JSON.parse(answer)

            if (Array.isArray(parsed) && parsed.every(value => typeof value === 'string')) {
              replayedAnswers = parsed
            }
          } catch {
            // Older/non-JSON replies remain a one-value replay below.
          }
        }

        const matchedChoices = options.filter(choice => replayedAnswers.includes(bareChoice(choice)))
        const note = lockedNotes?.[question.qid] ?? ''
        next[question.qid] =
          matchedChoices.length > 0
            ? {
                ...emptyStage,
                choices: matchedChoices,
                note,
                noteAnchor: note ? matchedChoices[0] : null,
                noteOpen: Boolean(note)
              }
            : { ...emptyStage, draft: answer, note, noteOpen: Boolean(note) }
      }

      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by the replay maps only
  }, [request?.lockedAnswers, request?.lockedNotes])

  const stageFor = useCallback((qid: string) => staged[qid] ?? emptyStage, [staged])

  const stagedAnswer = useCallback(
    (question: ClarifyQuestion): string | null => {
      const stage = staged[question.qid] ?? emptyStage

      if (stage.choices.length > 0) {
        return question.multiSelect ? JSON.stringify(stage.choices.map(bareChoice)) : bareChoice(stage.choices[0])
      }

      const draft = stage.draft.trim()

      return draft ? draft : null
    },
    [staged]
  )

  const answeredCount = questions.filter(q => stagedAnswer(q) !== null).length
  const allStaged = answeredCount === questions.length

  const confirmAll = useCallback(async () => {
    if (submittingRef.current) {
      return
    }

    if (!request || !gateway) {
      notifyError(new Error(request ? copy.gatewayDisconnected : copy.notReady), copy.sendFailed)

      return
    }

    submittingRef.current = true
    setSubmitting(true)

    try {
      // Sequential, not Promise.all: the LAST lock resolves the blocked tool
      // server-side, so every earlier lock must already be accepted when it
      // lands — a reordered burst could complete the batch with a missing
      // answer.
      //
      // Each lock rides the session's OWNER socket, not the ambient one: a
      // profile / Bot Chat switch re-points ambient at a backend that never
      // held this batch, which would leave the owner blocked.
      for (const question of questions) {
        const answer = stagedAnswer(question)
        const note = stageFor(question.qid).note.trim()

        await requestForOwnedSession<{ ok?: boolean }>(
          request.sessionId,
          gateway.request.bind(gateway) as typeof gateway.request,
          'clarify.respond',
          {
            answer: answer ?? '',
            ...(note ? { note } : {}),
            question_id: question.qid,
            request_id: request.requestId
          }
        )
      }

      triggerHaptic('submit')
      onAnswered()
      // tool.complete lands next → ClarifyToolBatchSettled.
      clearClarifyRequest(request.requestId, request.sessionId)
    } catch (error) {
      notifyError(error, copy.sendFailed)
      submittingRef.current = false
      setSubmitting(false)
    }
  }, [copy, gateway, onAnswered, questions, request, stageFor, stagedAnswer])

  const toggleChoice = useCallback((question: ClarifyQuestion, choice: string) => {
    setStaged(current => {
      const stage = current[question.qid] ?? emptyStage

      const next = question.multiSelect
        ? stage.choices.includes(choice)
          ? stage.choices.filter(value => value !== choice)
          : [...stage.choices, choice]
        : [choice]

      return {
        ...current,
        [question.qid]: {
          ...stage,
          choices: next,
          draft: '',
          noteAnchor:
            next.length === 0
              ? null
              : stage.noteAnchor && next.includes(stage.noteAnchor)
                ? stage.noteAnchor
                : stage.noteOpen
                  ? next[0]
                  : null
        }
      }
    })
  }, [])

  const draftFor = useCallback((question: ClarifyQuestion, value: string) => {
    setStaged(current => {
      const stage = current[question.qid] ?? emptyStage

      return { ...current, [question.qid]: { ...stage, choices: [], draft: value, noteAnchor: null } }
    })
  }, [])

  const noteFor = useCallback((question: ClarifyQuestion, value: string) => {
    setStaged(current => {
      const stage = current[question.qid] ?? emptyStage

      return { ...current, [question.qid]: { ...stage, note: value } }
    })
  }, [])

  const openNoteFor = useCallback((question: ClarifyQuestion, choice: string | null) => {
    setStaged(current => {
      const stage = current[question.qid] ?? emptyStage

      return { ...current, [question.qid]: { ...stage, noteAnchor: choice, noteOpen: true } }
    })
  }, [])

  const handleBatchFieldKey = useCallback(
    (question: ClarifyQuestion, event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.nativeEvent.isComposing || event.key !== 'Enter' || event.shiftKey) {
        return
      }

      event.preventDefault()

      if (!stagedAnswer(question)) {
        return
      }

      const currentIndex = questions.findIndex(item => item.qid === question.qid)
      const next = questions.slice(currentIndex + 1).find(item => stagedAnswer(item) === null)

      if (next) {
        const block = formRef.current?.querySelector(`[data-clarify-batch-question="${next.qid}"]`)

        ;(block?.querySelector<HTMLElement>('[data-choice], textarea') ?? null)?.focus()
      } else if (allStaged) {
        void confirmAll()
      }
    },
    [allStaged, confirmAll, questions, stagedAnswer]
  )

  const cancelAll = useCallback(async () => {
    if (!request) {
      return
    }

    onAnswered()
    clearClarifyRequest(request.requestId, request.sessionId)

    try {
      if (gateway) {
        // Owner-routed like the locks above — a skip sent to the wrong backend
        // is a silent no-op that leaves the agent waiting out its timeout.
        await requestForOwnedSession(
          request.sessionId,
          gateway.request.bind(gateway) as typeof gateway.request,
          'clarify.respond',
          { answer: '', request_id: request.requestId }
        )
      }
    } catch {
      // The tool times out on its own; a failed skip must never block the UI.
    }
  }, [gateway, onAnswered, request])

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()

      if (allStaged) {
        void confirmAll()
      }
    },
    [allStaged, confirmAll]
  )

  if (!ready) {
    return (
      <ClarifyShell aria-label={copy.loadingQuestion} className="my-1.5 grid min-h-12 place-items-center" role="status">
        <Loader2 aria-hidden className="size-4 animate-spin text-(--ui-text-tertiary)" />
      </ClarifyShell>
    )
  }

  return (
    <form className="my-1.5 grid gap-4" data-clarify-batch={questions.length} onSubmit={handleSubmit} ref={formRef}>
      <ClarifyShell className="grid gap-1.5">
        <div className="flex items-center gap-2 px-3">
          <MessageQuestion aria-hidden className="size-4 shrink-0 text-(--ui-text-tertiary)" />
          <span className="flex-1 text-[0.6875rem] font-medium leading-4 text-(--ui-text-secondary)">
            {copy.questionProgress(answeredCount, questions.length)}
          </span>
          {/* A segmented meter, not prose: with several questions the eye
              tracks remaining work off the bar rather than re-counting. */}
          <span aria-hidden className="flex shrink-0 items-center gap-1">
            {questions.map((question, index) => (
              <span
                className={cn(
                  'h-1 w-4 rounded-full transition-colors',
                  stagedAnswer(question) !== null ? 'bg-primary' : 'bg-(--ui-text-tertiary)/25'
                )}
                key={`meter-${question.qid}-${index}`}
              />
            ))}
          </span>
        </div>
        {questions.map((question, index) => (
          <BatchQuestionBlock
            disabled={submitting}
            index={index}
            key={question.qid}
            locked={false}
            onDraft={value => draftFor(question, value)}
            onFieldKeyDown={event => handleBatchFieldKey(question, event)}
            onNote={value => noteFor(question, value)}
            onNoteOpen={choice => openNoteFor(question, choice)}
            onToggle={choice => toggleChoice(question, choice)}
            question={question}
            request={request}
            staged={stageFor(question.qid)}
            total={questions.length}
          />
        ))}
      </ClarifyShell>

      <div className="flex items-center justify-end gap-1">
        <Button disabled={submitting} onClick={() => void cancelAll()} size="xs" type="button" variant="text">
          {copy.skip}
        </Button>
        <Button disabled={submitting || !allStaged} size="xs" type="submit">
          {submitting ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <>
              {copy.confirmAndContinueLabel}
              <span aria-hidden className="ml-0.5 text-[0.625rem] opacity-70">
                ⏎
              </span>
            </>
          )}
        </Button>
      </div>
    </form>
  )
}
