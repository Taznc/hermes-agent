import { useStore } from '@nanostores/react'

import { useI18n } from '@/i18n'

import { interactiveTerminalAvailable } from './capability'
import { TerminalSlot } from './persistent'
import { TerminalRail } from './rail'
import { $activeTerminalId, $terminals } from './terminals'

function TerminalUnavailableState() {
  const { t } = useI18n()

  return (
    <div
      className="flex min-h-0 min-w-0 flex-1 flex-col items-center justify-center gap-1 px-6 text-center"
      data-terminal-unavailable=""
    >
      <div className="text-[0.7rem] font-semibold uppercase tracking-[0.07em] text-muted-foreground/75">
        {t.rightSidebar.terminalUnavailableTitle}
      </div>
      <div className="max-w-72 text-[0.68rem] leading-relaxed text-muted-foreground/65">
        {t.rightSidebar.terminalUnavailableBody}
      </div>
    </div>
  )
}

/** Pane-side terminal chrome: the body slot (which the persistent overlay chases)
 *  plus the always-on tab rail. Lives in the real pane DOM — NOT the z-4 terminal
 *  overlay — so the rail sits above the collapsed sidebars' z-30 hover-reveal
 *  triggers (z-40, like the thread timeline) and suppresses them while hovered.
 *  The rail is always shown when a terminal exists (even one), so every tab keeps
 *  its close affordance; closing the last one hides the pane (reopen re-creates). */
export function TerminalPaneChrome() {
  const terminals = useStore($terminals)
  const activeId = useStore($activeTerminalId)
  const interactive = interactiveTerminalAvailable()
  const agentTerminals = terminals.filter(term => term.kind === 'agent')
  const activeAgent = agentTerminals.some(term => term.id === activeId)

  // A browser cannot host the Electron main process's node-pty bridge. Keep
  // agent-process mirrors usable, but never paint an empty shell-shaped surface
  // for an interactive terminal that cannot exist. Persisted user tabs may
  // coexist with agent mirrors, so key this on the ACTIVE supported tab.
  if (!interactive) {
    return (
      <div className="flex min-h-0 min-w-0 flex-1">
        <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          {activeAgent ? <TerminalSlot /> : <TerminalUnavailableState />}
        </div>
        {agentTerminals.length > 0 && <TerminalRail interactive={false} />}
      </div>
    )
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <TerminalSlot />
      </div>
      {terminals.length > 0 && <TerminalRail />}
    </div>
  )
}
