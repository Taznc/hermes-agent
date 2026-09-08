/**
 * Orchestration settings — the dashboard's dispatcher-knobs panel, flat-styled:
 * orchestrator profile, default assignee, auto-decompose, and the profile
 * descriptions the decomposer routes by (save / auto-generate per profile).
 */

import {
  Button,
  Codicon,
  host,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  $boardSlug,
  ALL_BOARDS,
  autoDescribeProfile,
  dispatchStatusKey,
  fetchDispatchStatus,
  fetchOrchestration,
  fetchProfiles,
  ORCHESTRATION_KEY,
  pauseDispatch,
  PROFILES_KEY,
  resumeDispatch,
  saveOrchestration,
  saveProfileDescription
} from './api'
import type { KanbanProfile } from './types'
import { errText, FIELD_LABEL, useKanban } from './ui'

const DEFAULT_SENTINEL = '__default__'

/**
 * Pause / drain / resume dispatch for the selected board.
 *
 * Workers run inside the gateway's cgroup, so restarting it while any are
 * running SIGKILLs them and discards uncommitted worktree progress. Pausing
 * fences only NEW claims and spawns — it never kills a live worker — so the
 * running count is the actual "is it safe to restart yet" signal, and it is
 * the prominent thing here.
 *
 * Board scope is explicit on both paths. Single-board requests use
 * `board=<slug>` (or the backend's active-board default); All Boards uses the
 * backend's aggregate `boards=*` contract, which returns per-board outcomes and
 * never resolves the sentinel to one hidden board.
 */
export function DispatchPauseControl() {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const isAllBoards = slug === ALL_BOARDS

  // Same 8s cadence the board's own drawer-adjacent polls use: while draining,
  // the operator is watching this number, so a 60s settings cadence is too slow.
  const { data: status } = useQuery({
    queryFn: fetchDispatchStatus,
    queryKey: dispatchStatusKey(slug),
    refetchInterval: 8_000
  })

  const refresh = () => void qc.invalidateQueries({ queryKey: dispatchStatusKey(slug) })

  const pause = useMutation({
    mutationFn: () => pauseDispatch(),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: result => {
      // A refusal arrives as a normal 200 with `paused: false` (a dispatch tick
      // owns the board lock). Reporting it as success would tell the operator a
      // board is draining when it is still claiming work.
      if (!result.paused) {
        host.notify({ kind: 'warning', message: k.pauseBusy })
      }

      refresh()
    }
  })

  const resume = useMutation({
    mutationFn: resumeDispatch,
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: result => {
      // Symmetric to pause: a contended board refuses with `resumed: false` and
      // HTTP 200. Silently refreshing there would leave the operator believing
      // dispatch had restarted while the board is still fenced.
      if (!result.resumed) {
        host.notify({ kind: 'warning', message: k.resumeBusy })
      }

      refresh()
    }
  })

  if (!status) {
    return null
  }

  const running = status.running_count
  const busy = pause.isPending || resume.isPending

  if (isAllBoards) {
    const boardCount = status.board_count ?? 0
    const pausedCount = status.paused_count ?? 0
    const allPaused = status.all_paused ?? false

    const scopeStatus = allPaused
      ? running === 0
        ? k.safeToRestart
        : k.draining(running)
      : pausedCount > 0
        ? k.boardsPaused(pausedCount, boardCount)
        : k.dispatchRunning

    const scopeTone =
      pausedCount === 0
        ? 'var(--ui-text-secondary)'
        : allPaused && running === 0
          ? 'var(--ui-text-positive)'
          : 'var(--ui-text-warning)'

    return (
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{k.dispatchControl}</span>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            disabled={busy || allPaused || boardCount === 0}
            onClick={() => pause.mutate()}
            size="xs"
            variant="outline"
          >
            <Codicon name="debug-pause" size="0.8rem" />
            {k.pauseAllBoards}
          </Button>
          <Button disabled={busy || pausedCount === 0} onClick={() => resume.mutate()} size="xs" variant="outline">
            <Codicon name="play" size="0.8rem" />
            {k.resumeAllBoards}
          </Button>
          <span className="text-[0.75rem] font-medium" style={{ color: scopeTone }}>
            {scopeStatus}
          </span>
        </div>
        <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.pauseHint}</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{k.dispatchControl}</span>
      <div className="flex flex-wrap items-center gap-3">
        {status.paused ? (
          <Button disabled={busy} onClick={() => resume.mutate()} size="xs" variant="outline">
            <Codicon name="play" size="0.8rem" />
            {k.resumeDispatch}
          </Button>
        ) : (
          <Button disabled={busy} onClick={() => pause.mutate()} size="xs" variant="outline">
            <Codicon name="debug-pause" size="0.8rem" />
            {k.pauseDispatch}
          </Button>
        )}
        {status.paused ? (
          <span
            className="text-[0.75rem] font-medium"
            style={{ color: running === 0 ? 'var(--ui-text-positive)' : 'var(--ui-text-warning)' }}
          >
            {running === 0 ? k.safeToRestart : k.draining(running)}
          </span>
        ) : (
          <span className="text-[0.75rem] text-(--ui-text-secondary)">{k.dispatchRunning}</span>
        )}
      </div>
      {/* The server renders the pause record (reason, who, when, note) so CLI,
          REST, and this panel can never drift into three phrasings of it. */}
      <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{status.message ?? k.pauseHint}</p>
    </div>
  )
}

function ProfilePicker({
  label,
  onSave,
  profiles,
  value
}: {
  label: string
  onSave: (name: string) => void
  profiles: KanbanProfile[]
  value: string
}) {
  const k = useKanban()

  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className={FIELD_LABEL}>{label}</span>
      <Select onValueChange={name => onSave(name === DEFAULT_SENTINEL ? '' : name)} value={value || DEFAULT_SENTINEL}>
        <SelectTrigger className="w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={DEFAULT_SENTINEL}>{k.defaultParen}</SelectItem>
          {profiles.map(profile => (
            <SelectItem key={profile.name} value={profile.name}>
              {profile.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  )
}

function ProfileDescriptionRow({ profile }: { profile: KanbanProfile }) {
  const k = useKanban()
  const qc = useQueryClient()
  const [draft, setDraft] = useState(profile.description)
  const invalidate = () => void qc.invalidateQueries({ queryKey: PROFILES_KEY })

  const save = useMutation({
    mutationFn: () => saveProfileDescription(profile.name, draft.trim()),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  const auto = useMutation({
    mutationFn: () => autoDescribeProfile(profile.name),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: result => {
      if (result.ok) {
        setDraft(result.description ?? '')
        invalidate()
      } else {
        host.notify({ kind: 'warning', message: result.reason || 'Auto-describe failed' })
      }
    }
  })

  return (
    <div className="flex items-center gap-2">
      <span className="w-24 shrink-0 truncate text-[0.75rem] font-medium text-(--ui-text-secondary)">
        {profile.name}
        {profile.is_default && (
          <span className="ml-1 text-[0.625rem] text-(--ui-text-quaternary)">{k.defaultParen}</span>
        )}
      </span>
      <Input
        className="h-7 flex-1 text-[0.71rem]"
        onChange={event => setDraft(event.target.value)}
        placeholder={k.profileGoodAt}
        value={draft}
      />
      <Button
        disabled={save.isPending || draft.trim() === profile.description}
        onClick={() => save.mutate()}
        size="xs"
        variant="outline"
      >
        {k.save}
      </Button>
      {/* Overlay the spinner so the button keeps its "Auto" width — the aux
          model can take a few seconds and a text swap would jump the row. */}
      <Button className="relative" disabled={auto.isPending} onClick={() => auto.mutate()} size="xs" variant="ghost">
        <span className={auto.isPending ? 'invisible' : ''}>{k.auto}</span>
        {auto.isPending && (
          <span className="absolute inset-0 grid place-items-center">
            <Codicon className="animate-spin [animation-duration:1.2s]" name="loading" size="0.75rem" />
          </span>
        )}
      </Button>
    </div>
  )
}

export function OrchestrationPanel() {
  const k = useKanban()
  const qc = useQueryClient()
  const { data: settings } = useQuery({ queryKey: ORCHESTRATION_KEY, queryFn: fetchOrchestration })
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) => saveOrchestration(patch),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ORCHESTRATION_KEY })
  })

  if (!settings || !roster) {
    return null
  }

  return (
    <div className="flex flex-col gap-4 border-t border-(--ui-stroke-tertiary) px-4 py-3">
      <div className="flex flex-wrap items-end gap-4">
        <ProfilePicker
          label={k.orchestratorProfile}
          onSave={name => save.mutate({ orchestrator_profile: name })}
          profiles={roster.profiles}
          value={settings.orchestrator_profile}
        />
        <ProfilePicker
          label={k.defaultAssignee}
          onSave={name => save.mutate({ default_assignee: name })}
          profiles={roster.profiles}
          value={settings.default_assignee}
        />
        <label className="flex cursor-pointer items-center gap-2 pb-1.5 text-[0.75rem] text-(--ui-text-secondary)">
          <Switch
            aria-label={k.autoDecompose}
            checked={settings.auto_decompose}
            onCheckedChange={checked => save.mutate({ auto_decompose: checked })}
            size="xs"
          />
          {k.autoDecompose}
        </label>
      </div>

      <DispatchPauseControl />

      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{k.profileDescriptions}</span>
        <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.profileDescriptionsHint}</p>
        {roster.profiles.map(profile => (
          <ProfileDescriptionRow key={`${profile.name}:${profile.description}`} profile={profile} />
        ))}
      </div>
    </div>
  )
}
