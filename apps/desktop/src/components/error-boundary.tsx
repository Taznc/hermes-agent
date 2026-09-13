import { Component, type ErrorInfo, type ReactNode, useState } from 'react'

import { Button } from '@/components/ui/button'
import { writeClipboardText } from '@/components/ui/copy-button'
import { ErrorState } from '@/components/ui/error-state'
import { useI18n } from '@/i18n'
import { performWebReload } from '@/store/web-reload'

export interface ErrorBoundaryFallbackProps {
  /** React component stack for the caught error, when the boundary captured one. */
  componentStack?: string
  error: Error
  reset: () => void
}

interface ErrorBoundaryProps {
  children: ReactNode
  fallback?: (props: ErrorBoundaryFallbackProps) => ReactNode
  label?: string
  onError?: (error: Error, info: ErrorInfo) => void
}

interface ErrorBoundaryState {
  componentStack?: string
  error: Error | null
}

// Some assistant-ui lookup races escape the message-local boundary and reach
// the root. Retry only that exact transient error class, never arbitrary render
// failures, and cap retries so a persistent failure still exposes the fallback.
const ASSISTANT_UI_LOOKUP_ERROR = /(useClientLookup|tapClient(Lookup|Resource)).*out of bounds/
const MAX_AUTO_RECOVERIES = 3
const AUTO_RECOVERY_WINDOW_MS = 5_000

const isTransientAssistantUiLookupError = (error: Error): boolean => ASSISTANT_UI_LOOKUP_ERROR.test(error.message)

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }
  private autoRecoveryCount = 0
  private autoRecoveryPending = false
  private autoRecoveryTimer: number | null = null
  private autoRecoveryWindowStart = 0

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidMount() {
    // StrictMode replays mount lifecycles in development. Its synthetic
    // componentWillUnmount clears the timer scheduled by componentDidCatch,
    // so restore the still-owned recovery on the matching remount.
    if (this.autoRecoveryPending && this.autoRecoveryTimer === null) {
      this.scheduleAutoRecovery()
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const label = this.props.label ?? ''
    const tag = label ? `[error-boundary:${label}]` : '[error-boundary]'
    console.error(tag, error, info.componentStack)

    // Kept in state so the fallback's Copy action can hand over the component
    // stack too — the console copy is unreachable for a non-devtools user.
    this.setState({ componentStack: info.componentStack ?? undefined })

    // Persist to desktop.log via Electron (#79428): console.error only reaches
    // the main process for windows with a console hook, is minified, and loses
    // the component stack. This survives the window and names the component.
    try {
      window.hermesDesktop?.reportRendererError?.({
        label: new URLSearchParams(window.location.search).get('win') ?? 'main',
        boundary: label || 'unlabeled',
        message: error.message,
        componentStack: info.componentStack ?? ''
      })
    } catch {
      // Logging must never take the boundary down with it.
    }

    this.props.onError?.(error, info)

    if (this.props.label === 'root' && isTransientAssistantUiLookupError(error) && this.takeAutoRecoveryAttempt()) {
      console.warn(`${tag} auto-recovering from assistant-ui lookup render race`, error.message)
      this.autoRecoveryPending = true
      this.scheduleAutoRecovery()
    }
  }

  componentWillUnmount() {
    this.clearAutoRecoveryTimer()
  }

  reset = () => {
    this.clearAutoRecoveryTimer()
    this.autoRecoveryPending = false
    this.autoRecoveryCount = 0
    this.autoRecoveryWindowStart = 0
    this.setState({ componentStack: undefined, error: null })
  }

  private takeAutoRecoveryAttempt(): boolean {
    const now = Date.now()

    if (this.autoRecoveryCount === 0 || now - this.autoRecoveryWindowStart >= AUTO_RECOVERY_WINDOW_MS) {
      this.autoRecoveryWindowStart = now
      this.autoRecoveryCount = 0
    }

    this.autoRecoveryCount += 1

    return this.autoRecoveryCount <= MAX_AUTO_RECOVERIES
  }

  private clearAutoRecoveryTimer() {
    if (this.autoRecoveryTimer !== null) {
      window.clearTimeout(this.autoRecoveryTimer)
      this.autoRecoveryTimer = null
    }
  }

  private scheduleAutoRecovery() {
    this.clearAutoRecoveryTimer()
    this.autoRecoveryTimer = window.setTimeout(this.autoRecover, 0)
  }

  private autoRecover = () => {
    this.autoRecoveryTimer = null
    this.autoRecoveryPending = false
    this.setState({ componentStack: undefined, error: null })
  }

  render() {
    const { componentStack, error } = this.state

    if (!error) {
      return this.props.children
    }

    if (this.props.fallback) {
      return this.props.fallback({ componentStack, error, reset: this.reset })
    }

    return <RootErrorFallback componentStack={componentStack} error={error} reset={this.reset} />
  }
}

export function RootErrorBoundary({ children }: { children: ReactNode }) {
  return <ErrorBoundary label="root">{children}</ErrorBoundary>
}

function RootErrorFallback({ componentStack, error, reset }: ErrorBoundaryFallbackProps) {
  const { t } = useI18n()
  // The toast host (NotificationStack) lives INSIDE <App/>, which this fallback
  // has replaced — notifyError() here would push into a store nobody renders,
  // so failures have to be reported inline or they are silently swallowed.
  const [logStatus, setLogStatus] = useState<string | null>(null)
  const [logLines, setLogLines] = useState<string[] | null>(null)
  const [copyStatus, setCopyStatus] = useState<'copied' | 'error' | 'idle'>('idle')

  const details = [error.message || t.errors.boundaryDesc, error.stack ?? '', componentStack ?? '']
    .filter(Boolean)
    .join('\n\n')

  const copyDetails = () => {
    void writeClipboardText(details)
      .then(() => setCopyStatus('copied'))
      .catch(() => setCopyStatus('error'))
  }

  // Electron reveals the folder; the web build has no host filesystem to open,
  // so fall back to reading the tail of the log INTO the page. Either way the
  // outcome is stated on screen instead of vanishing into a dead toast store.
  const openLogs = () => {
    setLogStatus(null)
    setLogLines(null)

    const bridge = window.hermesDesktop

    if (!bridge?.revealLogs) {
      setLogStatus(t.errors.openLogsFailed)

      return
    }

    void bridge
      .revealLogs()
      .then(result => {
        if (result?.ok) {
          setLogStatus(result.path || null)

          return
        }

        return Promise.resolve(bridge.getRecentLogs?.())
          .then(recent => {
            const lines = recent?.lines ?? []

            if (lines.length > 0) {
              setLogLines(lines.slice(-200))
              setLogStatus(recent?.path ?? null)

              return
            }

            setLogStatus(result?.error || t.errors.openLogsFailed)
          })
          .catch(() => setLogStatus(result?.error || t.errors.openLogsFailed))
      })
      .catch(err => setLogStatus(err instanceof Error ? err.message : t.errors.openLogsFailed))
  }

  return (
    <div
      className="fixed inset-0 z-(--z-crash) grid place-items-center overflow-auto bg-(--ui-chat-surface-background) p-6"
      // Masks a crashed app — must stay filled under window glass. Contract:
      // `[data-glass-opaque]` in styles.css.
      data-glass-opaque=""
    >
      <ErrorState
        className="w-full max-w-[28rem]"
        description={
          // body sets `user-select: none` app-wide; without this the user
          // cannot select or copy the one string that identifies the crash.
          <p
            className="max-w-prose text-center text-sm leading-5 whitespace-pre-wrap text-muted-foreground"
            data-selectable-text="true"
          >
            {error.message || t.errors.boundaryDesc}
          </p>
        }
        title={t.errors.boundaryTitle}
      >
        <Button className="font-semibold" onClick={reset} size="lg">
          {t.common.retry}
        </Button>
        <Button onClick={copyDetails} variant="text">
          {copyStatus === 'copied'
            ? t.common.copied
            : copyStatus === 'error'
              ? t.common.copyFailed
              : t.common.copy}
        </Button>
        <Button onClick={() => performWebReload()} variant="text">
          {t.errors.reloadWindow}
        </Button>
        <Button onClick={openLogs} variant="text">
          {t.errors.openLogs}
        </Button>

        {logStatus && (
          <p className="text-center text-xs break-words text-muted-foreground" data-selectable-text="true">
            {logStatus}
          </p>
        )}

        {logLines && (
          <pre
            className="max-h-64 overflow-auto rounded-md bg-black/20 p-2 text-left text-xs whitespace-pre-wrap"
            data-selectable-text="true"
          >
            {logLines.join('\n')}
          </pre>
        )}
      </ErrorState>
    </div>
  )
}
