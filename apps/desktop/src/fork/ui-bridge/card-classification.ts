import { registry } from '@/contrib/registry'
import { contributorForTool } from '@/fork/ui-bridge/store'
import { UI_REQUEST_AREA } from '@/fork/ui-bridge/types'

/** A plugin's durable result surface must not disappear into an activity summary.
 * Read the resolved registry so disabled, removed, and request-only contributions
 * follow the same rules as the inline slot that actually renders the card. */
export function isPluginResultCard(toolName: string): boolean {
  return contributorForTool(registry.getArea(UI_REQUEST_AREA), toolName) !== null
}
