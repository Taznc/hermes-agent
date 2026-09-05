/**
 * Behavior contract for the fork attachment-stream IPC registration: the
 * hermes:readFileChunkForAttach handler reads bounded chunks through the
 * hardened per-chunk reader against a REAL temp file, the renderer's
 * concatenation of chunks reproduces the file, and path authorization is
 * consulted before any read. The chunk reader itself is covered by
 * ../hardening tests; this file proves the wiring.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, expect, test } from 'vitest'

import { ATTACHMENT_CHUNK_BYTES } from '../hardening'
import { registerAttachmentStreamIpc } from './attachment-stream-ipc'

let dir: string

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-attachment-stream-ipc-'))
})

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true })
})

function handlerFor(overrides: { resolveRequestedPath?: (filePath: string, options: { purpose: string }) => string } = {}) {
  const handlers = new Map<string, (...args: any[]) => any>()

  registerAttachmentStreamIpc({
    ipcMain: { handle: (channel, listener) => void handlers.set(channel, listener) },
    resolveRequestedPath: overrides.resolveRequestedPath ?? (filePath => filePath),
    mimeTypeForPath: () => 'application/octet-stream'
  })

  expect(handlers.has('hermes:readFileChunkForAttach')).toBe(true)

  return handlers.get('hermes:readFileChunkForAttach')!
}

test('chunks concatenate back to the original file and each reply stays within the chunk bound', async () => {
  const filePath = path.join(dir, 'blob.bin')
  // Larger than one chunk so the renderer must loop.
  const original = Buffer.alloc(ATTACHMENT_CHUNK_BYTES + 128 * 1024)

  for (let i = 0; i < original.length; i++) {
    original[i] = i % 251
  }

  fs.writeFileSync(filePath, original)

  const handler = handlerFor()
  const pieces: Buffer[] = []
  let offset = 0

  for (;;) {
    const chunk = await handler(null, filePath, offset)

    expect(chunk.totalBytes).toBe(original.length)
    expect(chunk.bytesRead).toBeLessThanOrEqual(ATTACHMENT_CHUNK_BYTES)

    const bytes = Buffer.from(chunk.base64, 'base64')

    expect(bytes.length).toBe(chunk.bytesRead)
    pieces.push(bytes)
    offset += chunk.bytesRead

    if (offset >= chunk.totalBytes) {
      break
    }
  }

  expect(pieces.length).toBeGreaterThan(1)
  expect(Buffer.concat(pieces).equals(original)).toBe(true)
})

test('path authorization runs before the read and its rejection propagates', async () => {
  const filePath = path.join(dir, 'blob.bin')

  fs.writeFileSync(filePath, 'irrelevant')

  const handler = handlerFor({
    resolveRequestedPath: (_filePath, options) => {
      expect(options.purpose).toBe('Attachment upload')
      throw new Error('Attachment upload path not allowed')
    }
  })

  await expect(handler(null, filePath, 0)).rejects.toThrow('Attachment upload path not allowed')
})

test('a non-numeric offset reads from the start instead of throwing', async () => {
  const filePath = path.join(dir, 'small.txt')

  fs.writeFileSync(filePath, 'hello world')

  const handler = handlerFor()
  const chunk = await handler(null, filePath, 'not-a-number')

  expect(Buffer.from(chunk.base64, 'base64').toString('utf8')).toBe('hello world')
  expect(chunk.bytesRead).toBe(chunk.totalBytes)
})
