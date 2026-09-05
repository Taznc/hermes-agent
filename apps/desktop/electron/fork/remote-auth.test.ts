/**
 * Behavior contract for the fork remote-auth module: the static-token WS
 * ticket rung (token-auth remotes mint with `Authorization: Bearer <token>`,
 * never the loopback header) and the remote token-auth branch of
 * freshGatewayWsUrl (ticket URL on success, cached wsUrl on failure).
 */

import { expect, test } from 'vitest'

import { freshRemoteTokenWsUrl, mintWsTicketWithStaticToken } from './remote-auth'

test('mintWsTicketWithStaticToken POSTs the mint endpoint with the token as bearer', async () => {
  const calls: Array<{ url: string; token: string | null; options: any }> = []

  const ticket = await mintWsTicketWithStaticToken(
    'https://gw.example.com',
    { 'X-Custom': 'yes' },
    'secret-token',
    {
      fetchJson: async (url, token, options) => {
        calls.push({ url, token, options })

        return { ticket: 'minted-ticket' }
      }
    }
  )

  expect(ticket).toBe('minted-ticket')
  expect(calls).toHaveLength(1)
  expect(calls[0].url).toBe('https://gw.example.com/api/auth/ws-ticket')
  // The static token authenticates as a BEARER on the mint endpoint — it is
  // NOT passed as the loopback session token (a gated remote ignores that).
  expect(calls[0].token).toBeNull()
  expect(calls[0].options.bearer).toBe('secret-token')
  expect(calls[0].options.method).toBe('POST')
  expect(calls[0].options.headers).toEqual({ 'X-Custom': 'yes' })
})

test('mintWsTicketWithStaticToken throws when the gateway returns no usable ticket', async () => {
  for (const body of [{}, { ticket: 42 }, { ticket: '' }, null]) {
    await expect(
      mintWsTicketWithStaticToken('https://gw.example.com', {}, 'tok', { fetchJson: async () => body })
    ).rejects.toThrow('Gateway did not return a WS ticket.')
  }
})

const connection = {
  baseUrl: 'https://gw.example.com',
  headers: { 'X-Custom': 'yes' },
  token: 'secret-token',
  wsUrl: 'wss://gw.example.com/api/ws?token=secret-token'
}

test('freshRemoteTokenWsUrl returns a ticketed URL when the mint succeeds', async () => {
  const url = await freshRemoteTokenWsUrl(connection, {
    mintTicket: async (baseUrl, headers, staticToken) => {
      expect(baseUrl).toBe(connection.baseUrl)
      expect(headers).toEqual(connection.headers)
      expect(staticToken).toBe(connection.token)

      return 'minted-ticket'
    },
    buildTicketUrl: (baseUrl, ticket) => `${baseUrl.replace('https', 'wss')}/api/ws?ticket=${ticket}`
  })

  expect(url).toBe('wss://gw.example.com/api/ws?ticket=minted-ticket')
})

test('freshRemoteTokenWsUrl falls back to the cached wsUrl when minting fails', async () => {
  const url = await freshRemoteTokenWsUrl(connection, {
    mintTicket: async () => {
      throw new Error('mint endpoint absent (local/older backend)')
    },
    buildTicketUrl: () => {
      throw new Error('must not be called on mint failure')
    }
  })

  expect(url).toBe(connection.wsUrl)
})
