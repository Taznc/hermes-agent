import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import {
  cacheControlFor,
  distFileFor,
  IMMUTABLE,
  prodStaticMiddleware,
  REVALIDATE,
  rewriteRootUrl
} from './web-prod-serve'

let dist: string

beforeAll(() => {
  dist = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'web-prod-serve-'))
  fs.mkdirSync(path.join(dist, 'assets'))
  fs.writeFileSync(path.join(dist, 'index-web.html'), '<!doctype html>')
  fs.writeFileSync(path.join(dist, 'assets', 'main-abc123.js'), 'console.log(1)')
  fs.writeFileSync(path.join(dist, 'assets', 'main-abc123.js.br'), 'BR')
  fs.writeFileSync(path.join(dist, 'assets', 'logo-x.png'), 'png')
})

afterAll(() => fs.rmSync(dist, { recursive: true, force: true }))

function fakeRes() {
  const headers: Record<string, string | number> = {}

  return {
    headers,
    statusCode: 0,
    ended: false,
    setHeader(name: string, value: string | number) {
      headers[name.toLowerCase()] = value
    },
    end() {
      this.ended = true
    }
  }
}

describe('web-prod-serve (fork web desktop production server)', () => {
  it('rewrites / and /index.html to the web entry, keeping the token query', () => {
    expect(rewriteRootUrl('/')).toBe('/index-web.html')
    expect(rewriteRootUrl('/?token=t&profile=p')).toBe('/index-web.html?token=t&profile=p')
    expect(rewriteRootUrl('/index.html?token=t')).toBe('/index-web.html?token=t')
    expect(rewriteRootUrl('/api/status?x=1')).toBe('/api/status?x=1')
  })

  it('only resolves GET/HEAD files inside dist (no traversal, no /api)', () => {
    expect(distFileFor(dist, { url: '/assets/main-abc123.js', headers: {} })).toBe(
      path.join(dist, 'assets', 'main-abc123.js')
    )
    expect(distFileFor(dist, { url: '/../../etc/passwd', headers: {} })).toBeNull()
    expect(distFileFor(dist, { url: '/%2e%2e/%2e%2e/etc/passwd', headers: {} })).toBeNull()
    expect(distFileFor(dist, { url: '/api/status', headers: {} })).toBeNull()
    expect(distFileFor(dist, { url: '/assets/main-abc123.js', method: 'POST', headers: {} })).toBeNull()
    expect(distFileFor(dist, { url: '/assets', headers: {} })).toBeNull()
  })

  it('caches hashed assets forever and makes the entry revalidate', () => {
    expect(cacheControlFor(dist, path.join(dist, 'assets', 'main-abc123.js'))).toBe(IMMUTABLE)
    expect(cacheControlFor(dist, path.join(dist, 'index-web.html'))).toBe(REVALIDATE)
  })

  it('serves the brotli sibling when accepted, else falls through to sirv', async () => {
    const mw = prodStaticMiddleware(dist)

    const res = fakeRes()
    let fellThrough = false
    mw({ url: '/assets/main-abc123.js', method: 'HEAD', headers: { 'accept-encoding': 'gzip, br' } }, res, () => {
      fellThrough = true
    })
    expect(fellThrough).toBe(false)
    expect(res.headers['content-encoding']).toBe('br')
    expect(res.headers['cache-control']).toBe(IMMUTABLE)
    expect(res.headers['vary']).toBe('Accept-Encoding')

    const plain = fakeRes()
    let next = false
    mw({ url: '/assets/main-abc123.js', headers: { 'accept-encoding': 'identity' } }, plain, () => {
      next = true
    })
    expect(next).toBe(true)
    expect(plain.headers['content-encoding']).toBeUndefined()
    expect(plain.headers['cache-control']).toBe(IMMUTABLE)

    const api = fakeRes()
    let apiNext = false
    mw({ url: '/api/status', headers: { 'accept-encoding': 'br' } }, api, () => {
      apiNext = true
    })
    expect(apiNext).toBe(true)
    expect(api.headers).toEqual({})
  })
})
