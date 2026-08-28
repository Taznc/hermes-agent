import assert from 'node:assert/strict'

import { test } from 'vitest'

import { capMapSize, pruneExpiredEntries } from './bounded-cache'

test('capMapSize is a no-op below the limit', () => {
  const map = new Map([
    ['a', 1],
    ['b', 2]
  ])

  capMapSize(map, 5)

  assert.equal(map.size, 2)
})

test('capMapSize evicts oldest-first (Map insertion order) down to the limit', () => {
  const map = new Map<string, number>()

  for (const key of ['a', 'b', 'c', 'd']) {
    map.set(key, key.charCodeAt(0))
  }

  capMapSize(map, 2)

  assert.equal(map.size, 2)
  assert.deepEqual([...map.keys()], ['c', 'd'])
})

test('capMapSize handles an empty map without throwing', () => {
  const map = new Map<string, number>()

  assert.doesNotThrow(() => capMapSize(map, 0))
  assert.equal(map.size, 0)
})

test('capMapSize called repeatedly after each set keeps the map at the cap', () => {
  const map = new Map<number, number>()

  for (let i = 0; i < 100; i += 1) {
    map.set(i, i)
    capMapSize(map, 10)
  }

  assert.equal(map.size, 10)
  assert.deepEqual([...map.keys()], [90, 91, 92, 93, 94, 95, 96, 97, 98, 99])
})

test('pruneExpiredEntries deletes only entries past the TTL', () => {
  const now = 1_000_000
  const map = new Map([
    ['fresh', { at: now - 10 }],
    ['stale', { at: now - 100 }]
  ])

  pruneExpiredEntries(map, now, 50, value => value.at)

  assert.equal(map.has('fresh'), true)
  assert.equal(map.has('stale'), false)
})

test('pruneExpiredEntries also caps survivors when a limit is given', () => {
  const now = 1_000_000
  const map = new Map<string, { at: number }>()

  for (let i = 0; i < 5; i += 1) {
    map.set(`k${i}`, { at: now })
  }

  pruneExpiredEntries(map, now, 50, value => value.at, 3)

  assert.equal(map.size, 3)
  assert.deepEqual([...map.keys()], ['k2', 'k3', 'k4'])
})

test('pruneExpiredEntries with no limit leaves fresh entries uncapped', () => {
  const now = 1_000_000
  const map = new Map<string, { at: number }>()

  for (let i = 0; i < 5; i += 1) {
    map.set(`k${i}`, { at: now })
  }

  pruneExpiredEntries(map, now, 50, value => value.at)

  assert.equal(map.size, 5)
})
