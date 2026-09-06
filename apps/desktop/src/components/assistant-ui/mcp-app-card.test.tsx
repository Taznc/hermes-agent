import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { McpAppCard, mcpAppMessage, parseMcpAppCard } from './mcp-app-card'

const card = {
  html: '<!doctype html><html><body><button>Interactive chart</button></body></html>',
  id: 'per-call-opaque-identifier',
  resourceUri: 'ui://charts/summary',
  serverId: 'charts',
  toolName: 'chart'
}

describe('parseMcpAppCard', () => {
  it('accepts only the bounded typed payload emitted beside a normal tool result', () => {
    expect(parseMcpAppCard(card)).toEqual(card)
    expect(parseMcpAppCard({ ...card, resourceUri: 'https://evil.test' })).toBeNull()
    expect(parseMcpAppCard({ ...card, html: 'x'.repeat(300_000) })).toBeNull()
  })
})

describe('McpAppCard', () => {
  it('renders self-contained HTML in an opaque script-only frame without host capabilities', () => {
    const { container } = render(<McpAppCard card={card} />)
    const frame = container.querySelector('iframe')

    expect(frame).not.toBeNull()
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame?.getAttribute('allow')).toContain("camera 'none'")
    expect(frame?.srcdoc).toContain('Interactive chart')
    expect(frame?.srcdoc).not.toContain('window.hermes')
  })

  it('accepts only a bounded JSON-RPC size message with the mount token', () => {
    expect(mcpAppMessage({ jsonrpc: '2.0', method: 'ui/size', params: { height: 360 }, token: 'tok' }, 'tok')).toEqual({ height: 360 })
    expect(mcpAppMessage({ jsonrpc: '2.0', method: 'tools/call', params: {}, token: 'tok' }, 'tok')).toBeNull()
    expect(mcpAppMessage({ jsonrpc: '2.0', method: 'ui/size', params: { height: 360 }, token: 'wrong' }, 'tok')).toBeNull()
    expect(mcpAppMessage({ jsonrpc: '2.0', method: 'ui/size', params: { height: 360 }, token: 'tok', pad: 'x'.repeat(5000) }, 'tok')).toBeNull()
  })

  it('ignores a valid-looking message from any window other than its iframe', () => {
    const { container } = render(<McpAppCard card={card} />)
    const frame = container.querySelector('iframe') as HTMLIFrameElement
    window.dispatchEvent(new MessageEvent('message', {
      data: { jsonrpc: '2.0', method: 'ui/size', params: { height: 900 }, token: 'guessed' },
      source: window
    }))
    expect(frame.style.height).toBe('280px')
  })
})
