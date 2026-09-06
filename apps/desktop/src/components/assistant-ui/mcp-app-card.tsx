import { useEffect, useMemo, useRef, useState } from 'react'

import { collectThemeBridge, frameSizeFromMessage, themePrelude } from './inline-preview-directive'

const MAX_HTML_CHARS = 256 * 1024
const MAX_MESSAGE_CHARS = 4096
const DEFAULT_HEIGHT = 280

export interface McpAppCardPayload {
  html: string
  id: string
  resourceUri: string
  serverId: string
  toolName: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Parse the host-only envelope without accepting arbitrary result metadata. */
export function parseMcpAppCard(value: unknown): McpAppCardPayload | null {
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      return null
    }
  }
  if (!isRecord(value) || !isRecord(value.mcpApp)) {
    return null
  }

  const card = value.mcpApp
  if (
    typeof card.serverId !== 'string' ||
    typeof card.id !== 'string' ||
    card.id.length < 20 ||
    typeof card.toolName !== 'string' ||
    typeof card.resourceUri !== 'string' ||
    !card.resourceUri.startsWith('ui://') ||
    typeof card.html !== 'string' ||
    !card.html ||
    card.html.length > MAX_HTML_CHARS
  ) {
    return null
  }

  return { id: card.id, html: card.html, resourceUri: card.resourceUri, serverId: card.serverId, toolName: card.toolName }
}

export function mcpAppMessage(data: unknown, token: string): { height: number } | null {
  if (!isRecord(data) || data.jsonrpc !== '2.0' || data.method !== 'ui/size' || data.token !== token || !isRecord(data.params)) {
    return null
  }

  try {
    if (JSON.stringify(data).length > MAX_MESSAGE_CHARS) {
      return null
    }
  } catch {
    return null
  }

  const size = frameSizeFromMessage(
    { type: 'hermes-inline-preview-size', token, height: data.params.height, width: 0 },
    token
  )
  return size ? { height: size.height } : null
}

function mcpAppChrome(html: string, token: string): string {
  const { font, vars } = collectThemeBridge()
  const bridge = `<script>(function(){var t=${JSON.stringify(token)};var post=function(){var d=document.documentElement,b=document.body;var h=Math.max(d?d.scrollHeight:0,b?b.scrollHeight:0);parent.postMessage({jsonrpc:'2.0',method:'ui/size',params:{height:h},token:t},'*')};addEventListener('load',post);if(typeof ResizeObserver==='function'){new ResizeObserver(post).observe(document.documentElement)}post()})()</script>`
  const policy = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:\">"
  const close = /<\/body\s*>/i.exec(html)
  const framed = close ? html.slice(0, close.index) + bridge + html.slice(close.index) : html + bridge
  return policy + themePrelude(vars, font) + framed
}

export function McpAppCard({ card }: { card: McpAppCardPayload }) {
  const frameRef = useRef<HTMLIFrameElement>(null)
  const token = useMemo(() => crypto.randomUUID(), [])
  const [height, setHeight] = useState(DEFAULT_HEIGHT)
  const srcDoc = useMemo(() => mcpAppChrome(card.html, token), [card.html, token])

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frameRef.current?.contentWindow) {
        return
      }
      const size = mcpAppMessage(event.data, token)
      if (size) {
        setHeight(size.height)
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [token])

  return (
    <section aria-label={`MCP App from ${card.toolName}`} className="max-w-full overflow-hidden rounded-[0.25rem] border border-(--ui-stroke-tertiary)">
      <iframe
        allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
        className="block w-full border-0 bg-transparent"
        ref={frameRef}
        sandbox="allow-scripts"
        srcDoc={srcDoc}
        style={{ height }}
        title={`MCP App: ${card.toolName}`}
      />
    </section>
  )
}
