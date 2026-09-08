import { useStore } from '@nanostores/react'
import { type ComponentProps, type CSSProperties, type ReactNode, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { CopyButton } from '@/components/ui/copy-button'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { AlertCircle, AlertTriangle, CheckCircle2, type IconComponent, Info } from '@/lib/icons'
import { cn } from '@/lib/utils'
import {
  $notifications,
  type AppNotification,
  clearNotifications,
  dismissNotification,
  type NotificationKind
} from '@/store/notifications'

type ToneVariant = 'default' | 'destructive' | 'warning' | 'success'
type ActionVariant = ComponentProps<typeof Button>['variant']

interface Tone {
  icon: IconComponent
  iconClass: string
  /**
   * The Dismiss button's severity chrome, owned by `Button` as a named variant.
   * The call site selects a variant and passes no `bg-*`/`text-*`/hover classes
   * of its own — see DESIGN.md, "Style lives in the primitive".
   */
  actionVariant: ActionVariant
  variant: ToneVariant
}

const tone: Record<NotificationKind, Tone> = {
  error: {
    icon: AlertCircle,
    iconClass: 'text-destructive-text',
    actionVariant: 'severityError',
    variant: 'destructive'
  },
  warning: {
    icon: AlertTriangle,
    iconClass: 'text-warning-text',
    actionVariant: 'severityWarning',
    variant: 'warning'
  },
  info: {
    icon: Info,
    iconClass: 'text-muted-foreground',
    actionVariant: 'severityInfo',
    variant: 'default'
  },
  success: {
    icon: CheckCircle2,
    iconClass: 'text-success-text',
    actionVariant: 'severitySuccess',
    variant: 'success'
  }
}

// Preserve each alert variant's own semantic border/background instead of
// washing every kind into one overlay treatment. The shared stack class adds
// only the floating-surface behavior that is common across themes.
const STACK_SURFACE = 'pointer-events-auto shadow-nous'

function partitionNotifications(notifications: AppNotification[]) {
  const defaultStack: AppNotification[] = []
  const bottomRightStack: AppNotification[] = []

  for (const notification of notifications) {
    if (notification.placement === 'bottom-right') {
      bottomRightStack.push(notification)
    } else {
      defaultStack.push(notification)
    }
  }

  return { bottomRightStack, defaultStack }
}

export function NotificationStack() {
  const notifications = useStore($notifications)
  const { bottomRightStack, defaultStack } = partitionNotifications(notifications)
  const { t } = useI18n()
  const lastNotificationIdRef = useRef<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const copy = t.notifications

  useEffect(() => {
    if (defaultStack.length <= 1) {
      setExpanded(false)
    }
  }, [defaultStack.length])

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    const latest = notifications[0]

    if (!latest || latest.id === lastNotificationIdRef.current) {
      return
    }

    lastNotificationIdRef.current = latest.id

    if (latest.kind === 'success') {
      triggerHaptic('success')
    } else if (latest.kind === 'error') {
      triggerHaptic('error')
    } else if (latest.kind === 'warning') {
      triggerHaptic('warning')
    }
  }, [notifications])

  return (
    <>
      {defaultStack.length > 0 && (
        <TopCenterStack
          copy={copy}
          expanded={expanded}
          notifications={defaultStack}
          onToggleExpanded={() => setExpanded(v => !v)}
        />
      )}
      {bottomRightStack.length > 0 && <BottomRightStack copy={copy} notifications={bottomRightStack} />}
    </>
  )
}

// Portaled to <body> on the over-modal rung so a toast clears an open dialog —
// see the top-center variant below for why.
const REGION_BASE = 'pointer-events-none fixed z-(--z-over-modal) flex gap-2'

// Primary stack: top-center, collapsed to the latest toast with a "+N more"
// expander + clear-all — the noisy/important surface (errors, warnings,
// action toasts). Without the portal it lives inside the React root subtree,
// which any body-level dialog/overlay portal paints over — so a toast fired
// while a dialog is open was invisible.
function TopCenterStack({
  copy,
  expanded,
  notifications,
  onToggleExpanded
}: {
  copy: ReturnType<typeof useI18n>['t']['notifications']
  expanded: boolean
  notifications: AppNotification[]
  onToggleExpanded: () => void
}) {
  const [latest, ...older] = notifications

  return createPortal(
    <div
      aria-label={copy.region}
      className={cn(
        REGION_BASE,
        'left-1/2 top-[calc(var(--titlebar-height,34px)+0.75rem)] w-[min(32rem,calc(100%-2rem))] -translate-x-1/2 flex-col'
      )}
      role="region"
    >
      <NotificationItem notification={latest} />
      {expanded && older.map(n => <NotificationItem key={n.id} notification={n} />)}
      {older.length > 0 && (
        <div
          className={cn(
            STACK_SURFACE,
            'flex min-h-8 items-center justify-between rounded-lg border border-(--stroke-nous) bg-popover px-3 text-xs text-popover-foreground'
          )}
        >
          <Button className="-ml-2" onClick={onToggleExpanded} size="xs" type="button" variant="text">
            {expanded ? copy.hide : copy.show} {copy.more(older.length)}
          </Button>
          <Button className="-mr-2" onClick={clearNotifications} size="xs" type="button" variant="text">
            {copy.clearAll}
          </Button>
        </div>
      )}
    </div>,
    document.body
  )
}

// Ambient stack: bottom-right, every toast shown at once (routine confirmations
// rarely queue up), newest on top, no expand/clear-all chrome.
function BottomRightStack({
  copy,
  notifications
}: {
  copy: ReturnType<typeof useI18n>['t']['notifications']
  notifications: AppNotification[]
}) {
  return createPortal(
    <div
      aria-label={copy.region}
      className={cn(REGION_BASE, 'right-4 bottom-4 w-[min(24rem,calc(100%-2rem))] flex-col-reverse')}
      role="region"
    >
      {notifications.map(n => (
        <NotificationItem key={n.id} notification={n} />
      ))}
    </div>,
    document.body
  )
}

// Emphasize only the leading money figure ("$16.00" — the amount used) with the
// accent color (semibold), leaving the rest of the line in its default muted
// tone. No accent, or no figure in the message → render the text untouched.
function renderMessage(message: string, accent?: string): ReactNode {
  const match = accent ? /\$\d+(?:\.\d{2})?/.exec(message) : null

  if (!match) {
    return message
  }

  const start = match.index
  const end = start + match[0].length

  return (
    <>
      {message.slice(0, start)}
      <span className="font-semibold" style={{ color: accent }}>
        {match[0]}
      </span>
      {message.slice(end)}
    </>
  )
}

// AlertTitle defaults to a single-line clamp. Toast errors are often a full
// sentence, so let the title wrap naturally rather than hiding it in a tiny
// nested scroller or clipping it with an ellipsis.
export function toastTitleClassName() {
  return 'col-start-auto line-clamp-none whitespace-normal wrap-break-word text-[0.8125rem] leading-5'
}

function NotificationItem({ notification }: { notification: AppNotification }) {
  const styles = tone[notification.kind]
  const Icon = styles.icon
  const hasDetail = Boolean(notification.detail && notification.detail !== notification.message)
  const { t } = useI18n()
  const copy = t.notifications

  // Nudge the icon down to sit on the first text line, in `ch` so it tracks the
  // toast's font size instead of a fixed rem. `accentColor` (when set) tints the
  // icon + message as a severity ramp, overriding the kind's default color.
  const accent = notification.accentColor
  const iconStyle: CSSProperties = { marginTop: '0.42ch', ...(accent ? { color: accent } : {}) }

  return (
    <Alert
      aria-live={notification.kind === 'error' ? 'assertive' : 'polite'}
      className={cn(STACK_SURFACE, 'grid-cols-[auto_minmax(0,1fr)_auto] rounded-xl pr-2.5')}
      role={notification.kind === 'error' ? 'alert' : 'status'}
      variant={styles.variant}
    >
      {notification.icon ? (
        <Codicon className={styles.iconClass} name={notification.icon} size="1rem" style={iconStyle} />
      ) : (
        <Icon className={styles.iconClass} style={iconStyle} />
      )}
      <div className="col-start-2 min-w-0">
        {notification.title && (
          <AlertTitle className={toastTitleClassName()} title={notification.title}>
            {notification.title}
          </AlertTitle>
        )}
        <AlertDescription className="col-start-auto">
          <p
            className={cn(
              'm-0 max-w-prose wrap-break-word leading-relaxed text-(--ui-text-secondary)',
              notification.contextCard && 'line-clamp-3'
            )}
            data-notification-message={notification.contextCard ? 'contextual' : undefined}
          >
            {renderMessage(notification.message, accent)}
          </p>
          {notification.meta && <p className="m-0 text-xs text-muted-foreground tabular-nums">{notification.meta}</p>}
          {notification.contextCard && <NotificationContextCard card={notification.contextCard} />}
          {hasDetail && <NotificationDetail detail={notification.detail || ''} />}
          <div className="mt-2 flex w-full items-center justify-between gap-2">
            {notification.action && (
              <Button
                onClick={() => {
                  notification.action?.onClick()
                  dismissNotification(notification.id)
                }}
                size="default"
                type="button"
                variant="default"
              >
                {notification.action.label}
              </Button>
            )}
            <Button
              className="ml-auto"
              onClick={() => dismissNotification(notification.id)}
              size="default"
              type="button"
              variant={styles.actionVariant}
            >
              {copy.dismissAction}
            </Button>
          </div>
        </AlertDescription>
      </div>
      <Button
        aria-label={copy.dismiss}
        className="col-start-3 -mr-1 text-muted-foreground"
        onClick={() => dismissNotification(notification.id)}
        size="icon-xs"
        type="button"
        variant="ghost"
      >
        <Codicon name="close" size="0.875rem" />
      </Button>
    </Alert>
  )
}

/**
 * The toast-level equivalent of an object's compact card: deliberately quiet
 * and bounded, but visibly separate from the event that caused the notice.
 * The notification explains what happened; this block mirrors the affected
 * object's own card (name, summary, then quiet status/ID) so a terminal event
 * does not degrade into an opaque identifier.
 */
function NotificationContextCard({ card }: { card: NonNullable<AppNotification['contextCard']> }) {
  if (!card.title && !card.eyebrow && !card.summary && !card.meta) {
    return null
  }

  return (
    <div
      className="mt-2 grid gap-1 rounded-lg border border-(--ui-stroke-tertiary) border-l-2 bg-(--ui-bg-elevated) px-2.5 py-2"
      data-notification-context-card="true"
    >
      {card.title && <p className="m-0 wrap-break-word text-[0.8125rem] leading-5 font-medium text-foreground">{card.title}</p>}
      {card.summary && <p className="m-0 line-clamp-2 wrap-break-word text-xs leading-relaxed text-(--ui-text-secondary)">{card.summary}</p>}
      {(card.eyebrow || card.meta) && (
        <div className="flex min-w-0 items-center justify-between gap-2 text-[0.6875rem] text-(--ui-text-tertiary)">
          {card.eyebrow && <span className="min-w-0 truncate font-medium">{card.eyebrow}</span>}
          {card.meta && <span className="shrink-0 font-mono text-[0.625rem] text-(--ui-text-quaternary)">{card.meta}</span>}
        </div>
      )}
    </div>
  )
}

function NotificationDetail({ detail }: { detail: string }) {
  const { t } = useI18n()
  const copy = t.notifications

  return (
    <details className="mt-2 text-xs text-muted-foreground">
      <summary className="cursor-pointer select-none font-medium text-muted-foreground hover:text-foreground">
        {copy.details}
      </summary>
      <div className="mt-1 rounded-md bg-background/65 p-2">
        <pre
          className="max-h-32 whitespace-pre-wrap wrap-break-word font-mono text-[0.6875rem] leading-relaxed"
          data-selectable-text="true"
        >
          {detail}
        </pre>
        <CopyButton
          appearance="inline"
          className="mt-1 rounded px-1.5 py-0.5 text-[0.6875rem]"
          errorMessage={copy.copyDetailFailed}
          iconClassName="size-3"
          label={copy.copyDetail}
          text={detail}
        >
          {copy.copyDetail}
        </CopyButton>
      </div>
    </details>
  )
}

export function InlineNotice({
  kind = 'info',
  title,
  children,
  className
}: {
  kind?: NotificationKind
  title?: string
  children: ReactNode
  className?: string
}) {
  const styles = tone[kind]
  const Icon = styles.icon

  return (
    <Alert className={cn('min-w-0', className)} role={kind === 'error' ? 'alert' : 'status'} variant={styles.variant}>
      <Icon />
      {title && <AlertTitle>{title}</AlertTitle>}
      <AlertDescription className={cn(!title && 'row-start-1')}>{children}</AlertDescription>
    </Alert>
  )
}
