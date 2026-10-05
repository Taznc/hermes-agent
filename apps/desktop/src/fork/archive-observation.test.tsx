import { readFileSync } from 'node:fs'

import { useStore } from '@nanostores/react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { type ReactNode, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { afterEach, expect, it, vi } from 'vitest'

import { $gateway, requestGatewayForProfile } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'

import { forkHost } from './sdk-host'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForProfile: vi.fn()
}))

const source = process.env.FORK_SIDEBAR_ARCHIVE_PLUGIN
  ? readFileSync(process.env.FORK_SIDEBAR_ARCHIVE_PLUGIN, 'utf8')
  : `export default { register(ctx) { ctx.register({ data: { render: ({sessionId}) => jsx(HostRow, {id:sessionId}) } }) } }`

function HostRow({ id }: { id: string }) {
  const blockers = useStore(forkHost.sessions.archiveBlockers)
  useEffect(() => forkHost.sessions.observeArchive(id), [id])

  return blockers[id] ? null : <span aria-label="Archive session" />
}

const plugin = new Function(
  'HostRow',
  'jsx',
  'jsxs',
  'useEffect',
  'useState',
  'Codicon',
  'ConfirmDialog',
  'haptic',
  'host',
  'SESSION_ROW_AREAS',
  'Tip',
  'useValue',
  source
    .replace(/^import .+ from .+$/gm, '')
    .replace(/export function /g, 'function ')
    .replace(/export default /g, 'return ')
)(
  HostRow,
  jsx,
  jsxs,
  useEffect,
  useState,
  () => null,
  () => null,
  () => undefined,
  { fork: forkHost },
  { trailing: 'row' },
  ({ children }: { children: ReactNode }) => children,
  useStore
)

const registrations: { data: { render: (props: { sessionId: string }) => ReactNode } }[] = []
plugin.register({ register: (entry: (typeof registrations)[number]) => registrations.push(entry) })

function Row({ id }: { id: string }) {
  return registrations[0].data.render({ sessionId: id })
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  $sessions.set([])
  $gateway.set(null)
  $activeGatewayProfile.set('default')
})
it('hides backend-only work until it settles and stops reading after unmount', async () => {
  vi.useFakeTimers()
  $sessions.set([{ id: 'tip', profile: 'other', _lineage_root_id: 'root' }] as never)
  vi.mocked(requestGatewayForProfile).mockResolvedValue({
    archivable: false,
    blockers: ['descendant'],
    session_key: 'root'
  })
  const view = render(<Row id="tip" />)
  await act(() => vi.advanceTimersByTimeAsync(0))
  expect(forkHost.sessions.archiveBlockers.get()).toEqual({ tip: 'backend-work', root: 'backend-work' })
  expect(screen.queryByLabelText('Archive session')).toBeNull()
  expect(requestGatewayForProfile).toHaveBeenCalledWith(
    'other',
    'fork.session.archive_status',
    {
      session_id: 'tip',
      profile: 'other'
    },
    5000
  )
  vi.mocked(requestGatewayForProfile).mockResolvedValue({ archivable: true, blockers: [], session_key: 'root' })
  await act(() => vi.advanceTimersByTimeAsync(5000))
  expect(screen.getByLabelText('Archive session')).toBeTruthy()
  view.unmount()
  vi.mocked(requestGatewayForProfile).mockClear()
  await act(() => vi.advanceTimersByTimeAsync(10000))
  expect(requestGatewayForProfile).not.toHaveBeenCalled()
})

it.each([new Error('offline'), { archivable: 'yes' }])(
  'fails closed on invalid discovery and recovers on a later sweep: %s',
  async failure => {
    vi.useFakeTimers()
    const probe = vi.mocked(requestGatewayForProfile).mockReset()

    if (failure instanceof Error) {
      probe.mockRejectedValue(failure)
    } else {
      probe.mockResolvedValue(failure)
    }

    render(<Row id="failure" />)
    await act(() => vi.advanceTimersByTimeAsync(0))
    expect(screen.queryByLabelText('Archive session')).toBeNull()
    probe.mockResolvedValue({ archivable: true })
    await act(() => vi.advanceTimersByTimeAsync(5000))
    expect(screen.getByLabelText('Archive session')).toBeTruthy()
  }
)
it('feature detects missing RPC once per profile, without permanently hiding idle icons', async () => {
  vi.useFakeTimers()

  const probe = vi
    .mocked(requestGatewayForProfile)
    .mockReset()
    .mockRejectedValue(Object.assign(new Error('Method not found'), { code: -32601 }))

  render(<Row id="legacy" />)
  await act(() => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByLabelText('Archive session')).toBeTruthy()
  await act(() => vi.advanceTimersByTimeAsync(10000))
  expect(probe).toHaveBeenCalledTimes(1)
})
it('drops cached old-backend capability when the gateway reconnects', async () => {
  vi.useFakeTimers()

  const probe = vi
    .mocked(requestGatewayForProfile)
    .mockReset()
    .mockRejectedValue(Object.assign(new Error('Method not found'), { code: -32601 }))

  render(<Row id="reconnect" />)
  await act(() => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByLabelText('Archive session')).toBeTruthy()
  probe.mockResolvedValue({ archivable: false })
  act(() => $gateway.set({} as never))
  expect(screen.queryByLabelText('Archive session')).toBeNull()
  await act(() => vi.advanceTimersByTimeAsync(5000))
  expect(probe).toHaveBeenCalledTimes(2)
  expect(screen.queryByLabelText('Archive session')).toBeNull()
})
it('ignores a stale profile response across A -> B -> A and re-probes each scope', async () => {
  vi.useFakeTimers()
  $activeGatewayProfile.set('a')
  let resolve!: (value: unknown) => void

  const pending = new Promise(r => {
    resolve = r
  })

  const probe = vi
    .mocked(requestGatewayForProfile)
    .mockReset()
    .mockReturnValueOnce(pending)
    .mockResolvedValue({ archivable: false })

  render(<Row id="same" />)
  await act(() => vi.advanceTimersByTimeAsync(0))
  act(() => $activeGatewayProfile.set('b'))
  await act(async () => {
    resolve({ archivable: true })
    await pending
  })
  await act(() => vi.advanceTimersByTimeAsync(1))
  expect(screen.queryByLabelText('Archive session')).toBeNull()
  expect(probe.mock.calls.map(call => call[0])).toEqual(['a', 'b'])
  probe.mockResolvedValue({ archivable: true })
  act(() => $activeGatewayProfile.set('a'))
  await act(() => vi.advanceTimersByTimeAsync(5000))
  expect(screen.getByLabelText('Archive session')).toBeTruthy()
})
it('dedupes mounted lineage aliases and bounds one shared sweep to 16 reads / four concurrent requests', async () => {
  vi.useFakeTimers()
  $sessions.set([{ id: 'tip', profile: 'other', _lineage_root_id: 'root' }] as never)
  let active = 0
  let peak = 0

  const probe = vi
    .mocked(requestGatewayForProfile)
    .mockReset()
    .mockImplementation(async () => {
      active++
      peak = Math.max(peak, active)
      await new Promise(resolve => setTimeout(resolve, 1))
      active--

      return { archivable: true }
    })

  render(
    <>
      <Row id="tip" />
      <Row id="root" />
      {Array.from({ length: 20 }, (_, i) => (
        <Row id={`s${i}`} key={i} />
      ))}
    </>
  )
  await act(() => vi.advanceTimersByTimeAsync(100))
  expect(probe).toHaveBeenCalledTimes(16)
  expect(peak).toBe(4)
  expect(probe.mock.calls.filter(call => call[2]?.session_id === 'tip')).toHaveLength(1)
  await act(() => vi.advanceTimersByTimeAsync(5100))
  expect(screen.getAllByLabelText('Archive session')).toHaveLength(22)
})
