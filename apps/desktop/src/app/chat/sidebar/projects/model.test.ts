import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { latestProjectSessions, orderProjectsByIds, sortProjectsForOverview } from './model'
import { NO_PROJECT_ID, type SidebarProjectTree } from './workspace-groups'

function makeProject(id: string, sessionCount: number): SidebarProjectTree {
  return {
    id,
    isAuto: true,
    label: id,
    lastActive: 0,
    path: `/repos/${id}`,
    previewSessions: [],
    repos: [],
    sessionCount
  }
}

const home = (): SidebarProjectTree => ({
  ...makeProject(NO_PROJECT_ID, 2),
  isAuto: false,
  isNoProject: true,
  path: null
})

const ids = (projects: SidebarProjectTree[]) => projects.map(project => project.id)

describe('orderProjectsByIds', () => {
  it('leaves the deterministic sort alone when nothing has been dragged', () => {
    const projects = [makeProject('a', 0), makeProject('b', 2)]

    expect(orderProjectsByIds(projects, [])).toBe(projects)
  })

  it('applies the saved manual order', () => {
    const projects = [makeProject('a', 1), makeProject('b', 1), makeProject('c', 1)]

    expect(ids(orderProjectsByIds(projects, ['c', 'a', 'b']))).toEqual(['c', 'a', 'b'])
  })

  it('keeps freshly-scanned zero-session repos below the hand-ordered list', () => {
    // The regression: a disk scan keeps finding git checkouts the user has
    // never opened in Hermes. Surfacing every unsaved id at the top buried the
    // projects they deliberately dragged into place.
    const projects = [makeProject('scanned-1', 0), makeProject('mine', 4), makeProject('scanned-2', 0)]

    expect(ids(orderProjectsByIds(projects, ['mine']))).toEqual(['mine', 'scanned-1', 'scanned-2'])
  })

  it('still surfaces a new project that has real activity', () => {
    // A project you just started working in should not sink beneath the saved
    // order — only the zero-session discoveries do.
    const projects = [makeProject('ordered', 1), makeProject('just-started', 3)]

    expect(ids(orderProjectsByIds(projects, ['ordered']))).toEqual(['just-started', 'ordered'])
  })

  it('drops ids that are no longer present', () => {
    const projects = [makeProject('a', 1)]

    expect(ids(orderProjectsByIds(projects, ['gone', 'a']))).toEqual(['a'])
  })

  it('keeps Home on top of a hand-picked order', () => {
    const projects = [makeProject('a', 1), home(), makeProject('b', 1)]

    expect(ids(orderProjectsByIds(projects, ['b', 'a']))).toEqual([NO_PROJECT_ID, 'b', 'a'])
  })
})

describe('sortProjectsForOverview', () => {
  it('puts Home above the active project', () => {
    const active = { ...makeProject('active', 5), isAuto: false }
    const projects = [makeProject('scanned', 0), active, home()]

    expect(ids(sortProjectsForOverview(projects, 'active'))).toEqual([NO_PROJECT_ID, 'active', 'scanned'])
  })
})

describe('latestProjectSessions', () => {
  const sessionRow = (id: string, overrides: Partial<SessionInfo> = {}): SessionInfo =>
    ({ archived: false, id, last_active: 0, message_count: 1, source: 'cli', started_at: 0, title: id, ...overrides }) as SessionInfo

  const projectWithSessions = (sessions: SessionInfo[]): SidebarProjectTree => ({
    ...makeProject('/www/app', sessions.length),
    repos: [
      {
        groups: [{ id: '/www/app::main', isMain: true, label: 'main', path: '/www/app', sessions }],
        id: '/www/app',
        label: 'app',
        path: '/www/app',
        sessionCount: sessions.length
      }
    ]
  })

  it('drops a session flagged archived on the bare-flag default', () => {
    const project = projectWithSessions([sessionRow('live'), sessionRow('archived-flag', { archived: true })])

    expect(latestProjectSessions(project, 8).map(s => s.id)).toEqual(['live'])
  })

  it('rejects a stale archived=false row via an injected archive predicate', () => {
    // Round-2 review finding 3 (t_d0a6300e): a caller wired into the
    // centralized `$sidebarIsArchivedSession` policy must be able to reject a
    // row the bare-flag default cannot see (still stamped `archived: false`
    // locally, but `archived: true` per the independent archived-only query).
    const project = projectWithSessions([sessionRow('live'), sessionRow('stale-archived-false')])
    const isArchived = (session: SessionInfo) => session.id === 'stale-archived-false'

    expect(latestProjectSessions(project, 8, isArchived).map(s => s.id)).toEqual(['live'])
  })
})
