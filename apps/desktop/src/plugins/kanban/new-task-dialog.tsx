/**
 * The two dialogs that create board content from scratch: `NewTaskDialog`
 * (full task creation, all boards + workspace + model override + pasted
 * images) and `IdeaCaptureDialog` (the board header's free-typed roadmap
 * idea capture, Phase 2.15). Kept together — both are small, self-contained
 * write dialogs invoked from `KanbanBoardPage`'s header/footer, and neither
 * earns its own file on size alone.
 */

import {
  Button,
  Codicon,
  compactNumber,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  host,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { type ClipboardEvent as ReactClipboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'

import {
  $boardSlug,
  addRoadmapIdea,
  ALL_BOARDS,
  BOARDS_KEY,
  createTask,
  deleteStagedAttachment,
  estimateNew,
  fetchBoards,
  fetchProfiles,
  patchTask,
  PROFILES_KEY,
  stageAttachment
} from './api'
import { EMPTY_OVERRIDE, ModelOverrideField, overrideCreateFields, type TaskModelOverride } from './model-override'
import { PriorityPicker } from './priority-picker'
import { type TaskEstimate } from './types'
import { columnLabel, errText, FIELD_LABEL, useKanban, useOrchestration } from './ui'

const NO_PARENT = '__none__'
const PARKED = '__parked__'
const WORKSPACE_KINDS = ['scratch', 'worktree', 'dir'] as const

function Field({ children, label }: { children: ReactNode; label: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className={FIELD_LABEL}>{label}</span>
      {children}
    </label>
  )
}

// One image pasted into the new-task dialog before the task exists — staged
// server-side immediately (see api.ts's stageAttachment), previewed locally
// via an object URL, and promoted into a real attachment on submit via its
// `token`. `blob` is kept so a board switch in All Boards mode can re-stage
// the same bytes against the newly chosen board (staged blobs live in the
// target board's own staging DB, so a token from board A never promotes on
// board B).
interface PendingImage {
  token: string
  filename: string
  previewUrl: string
  size: number
  blob: Blob
  /** The board this token is staged against ('' = the server's active board,
   *  matching `boardPath`'s "no board param" fallback). */
  board: string
}

export function NewTaskDialog({
  onClose,
  parents,
  target
}: {
  onClose: () => void
  /** Candidate parent tasks. Each carries its own `board` in All Boards mode
   *  (absent in single-board mode) so the picker can offer only parents on the
   *  board the new card will actually be created on — a link across boards is
   *  rejected by the backend, which owns one board's DB per request. */
  parents: Array<{ id: string; title: string; board?: null | string }>
  target: null | string
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })
  // Title-only creates must RUN: "auto" resolves to the orchestration default
  // (ultimately the active profile), applied at create time. Never silently
  // unassigned — parking a card is the explicit choice, not the default.
  const resolvedDefault = useOrchestration()?.resolved_default_assignee || 'default'

  // Board-level workspace default: a task inherits the current board's
  // configured project dir (scratch when unset, worktree in a git repo, else
  // dir) unless the operator overrides it below. Set the board default in the
  // board switcher's "Board settings…".
  const selectedSlug = useValue($boardSlug)
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })

  // In All Boards mode `$boardSlug` is the sentinel, which resolves to NO
  // board on the wire — the server would then silently create the card on
  // whatever board is active. So the dialog asks: an explicit picker, defaulted
  // to the server's own current board, and the chosen slug is threaded through
  // every write below (create, the follow-up status patch, and image staging).
  const isAllBoards = selectedSlug === ALL_BOARDS
  const [targetBoard, setTargetBoard] = useState('')
  // The board every write in this dialog goes to. Outside All Boards mode this
  // stays `undefined`, so `boardPath` falls through to `$boardSlug` exactly as
  // it always did — single-board behavior is byte-for-byte unchanged.
  const writeBoard = isAllBoards ? targetBoard : undefined
  const effectiveSlug = isAllBoards ? targetBoard : selectedSlug || boards?.current || ''
  const currentBoard = boards?.boards.find(b => b.slug === (effectiveSlug || boards.current))
  const boardDefaultKind = currentBoard?.default_workspace_kind || 'scratch'
  const boardDefaultDir = currentBoard?.default_workdir || ''

  // Parents must live on the board the card is created on — the backend link
  // write sees one board's DB. In single-board mode nothing carries a `board`
  // and every option stays offered, exactly as before.
  const parentOptions = useMemo(
    () => (isAllBoards ? parents.filter(option => (option.board ?? '') === targetBoard) : parents),
    [isAllBoards, parents, targetBoard]
  )

  const isTriage = target === 'triage'
  const [title, setTitle] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [assignee, setAssignee] = useState('')
  const [priority, setPriority] = useState(0)
  const [skills, setSkills] = useState('')
  const [workspaceKind, setWorkspaceKind] = useState<string>(boardDefaultKind)
  // Empty = inherit the board's default project dir (backend resolves it);
  // a path here overrides just this task. Only meaningful for dir/worktree.
  const [workspacePath, setWorkspacePath] = useState('')
  const [parent, setParent] = useState('')
  const [modelOverride, setModelOverride] = useState<TaskModelOverride>(EMPTY_OVERRIDE)
  const [goalMode, setGoalMode] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)
  const [estimate, setEstimate] = useState<null | TaskEstimate>(null)
  // Images pasted (Cmd/Ctrl+V) into the dialog before the task exists — each
  // is uploaded to the staging endpoint immediately so the create-task call
  // only ever carries small tokens, never raw bytes. `uploading` tracks
  // in-flight paste uploads so the create button can wait for them.
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([])
  const [uploadingImages, setUploadingImages] = useState(0)
  const pendingImagesRef = useRef<PendingImage[]>([])
  pendingImagesRef.current = pendingImages

  // Rough effort estimate from the typed title/body (before the task exists),
  // via the auto-routed auxiliary model. Makes a model call — explicit action.
  const estMut = useMutation({
    mutationFn: () => estimateNew(title.trim(), bodyText.trim()),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: r => {
      if (r.ok) {
        setEstimate(r)
      } else {
        host.notify({ kind: 'warning', message: r.reason || k.couldNotEstimate })
      }
    }
  })

  // Reset per open — the dialog is externally controlled (open = target set),
  // so onOpenChange(true) never fires; key the reset off `target` (and the
  // resolved board default, which may arrive after the first open).
  useEffect(() => {
    if (target) {
      setTitle('')
      setBodyText('')
      setAssignee('')
      setPriority(0)
      setSkills('')
      setWorkspaceKind(boardDefaultKind)
      setWorkspacePath('')
      setParent('')
      setModelOverride(EMPTY_OVERRIDE)
      setGoalMode(false)
      setError(null)
      setBusy(false)
      setEstimate(null)
      setPendingImages([])
      setUploadingImages(0)
    }
  }, [target, boardDefaultKind])

  // Default the All Boards picker to the server's own current board, so the
  // pre-selected target matches what a single-board create would have done —
  // the difference is that it is now VISIBLE and changeable, never silent.
  // Only while the dialog is open, and only until the user picks something.
  const serverCurrent = boards?.current ?? ''

  useEffect(() => {
    if (target && isAllBoards && !targetBoard && serverCurrent) {
      setTargetBoard(serverCurrent)
    }
  }, [target, isAllBoards, targetBoard, serverCurrent])

  // Best-effort cleanup for images pasted but never submitted: revoke the
  // local object URLs (avoid leaking blob: refs) and delete the staged
  // blobs server-side. Not required for correctness — the TTL reaper cleans
  // up abandoned staged uploads regardless — but keeps the board tidy
  // immediately. Fire-and-forget: a failure here shouldn't block closing.
  const cleanupPending = () => {
    for (const image of pendingImagesRef.current) {
      URL.revokeObjectURL(image.previewUrl)
      deleteStagedAttachment(image.token, image.board || undefined).catch(() => undefined)
    }
  }

  const handleClose = () => {
    cleanupPending()
    onClose()
  }

  /** Stage one image's bytes against `board` and return the pending row. */
  const stageImage = (blob: Blob, filename: string, previewUrl: string, board: string) =>
    blob
      .arrayBuffer()
      .then(bytes => stageAttachment({ bytes, contentType: blob.type || undefined, filename }, board || undefined))
      .then(({ attachment }) => ({
        blob,
        board,
        filename: attachment.filename,
        previewUrl,
        size: attachment.size,
        token: attachment.token
      }))

  // Paste handler: pull image items off the clipboard, upload each straight
  // to the staging endpoint (before Create is ever clicked), and show a
  // thumbnail immediately. Non-image clipboard data (plain text, etc.) is
  // left alone so normal paste-into-textarea keeps working.
  const handlePaste = (event: ReactClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(event.clipboardData?.items ?? []).filter(item => item.type.startsWith('image/'))

    if (items.length === 0) {
      return
    }

    event.preventDefault()

    for (const item of items) {
      const blob = item.getAsFile()

      if (!blob) {
        continue
      }

      const previewUrl = URL.createObjectURL(blob)

      const filename =
        blob.name || `pasted-image-${Date.now()}.${(blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`

      setUploadingImages(count => count + 1)

      stageImage(blob, filename, previewUrl, writeBoard ?? '')
        .then(image => setPendingImages(images => [...images, image]))
        .catch(err => {
          URL.revokeObjectURL(previewUrl)
          host.notify({ kind: 'error', message: `${k.imagePasteFailed}: ${errText(err)}` })
        })
        .finally(() => setUploadingImages(count => count - 1))
    }
  }

  // A staged blob lives in ITS board's staging DB, so switching the target
  // board after pasting would leave the token unresolvable at promotion —
  // the image would vanish from the created card with only a warning. Re-stage
  // the bytes we still hold against the new board and drop the old token.
  useEffect(() => {
    if (!target || !isAllBoards || !targetBoard) {
      return
    }

    const stale = pendingImagesRef.current.filter(image => image.board !== targetBoard)

    if (stale.length === 0) {
      return
    }

    for (const image of stale) {
      setUploadingImages(count => count + 1)

      stageImage(image.blob, image.filename, image.previewUrl, targetBoard)
        .then(restaged => {
          deleteStagedAttachment(image.token, image.board || undefined).catch(() => undefined)
          setPendingImages(images => images.map(candidate => (candidate.token === image.token ? restaged : candidate)))
        })
        .catch(err => host.notify({ kind: 'error', message: `${k.imagePasteFailed}: ${errText(err)}` }))
        .finally(() => setUploadingImages(count => count - 1))
    }
    // `stageImage` closes over nothing that changes per render besides the
    // board it is passed explicitly; re-running on every render would re-stage
    // in a loop. Keyed strictly on the board actually switching.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, isAllBoards, targetBoard])

  const removePendingImage = (token: string) => {
    const image = pendingImages.find(candidate => candidate.token === token)

    if (image) {
      URL.revokeObjectURL(image.previewUrl)
    }

    setPendingImages(images => images.filter(candidate => candidate.token !== token))
    deleteStagedAttachment(token, image?.board || undefined).catch(() => undefined)
  }

  // A parent chosen before the board switched now belongs to another board and
  // would be rejected as a link target. Drop it rather than sending it.
  useEffect(() => {
    if (parent && !parentOptions.some(option => option.id === parent)) {
      setParent('')
    }
  }, [parent, parentOptions])

  const submit = async () => {
    const trimmed = title.trim()

    if (!trimmed || !target || busy) {
      return
    }

    // Never create without a resolved board in All Boards mode: the sentinel
    // carries no board and the server would pick the active one silently.
    if (isAllBoards && !targetBoard) {
      setError(k.pickBoard)

      return
    }

    setBusy(true)
    setError(null)

    try {
      const skillList = skills
        .split(',')
        .map(s => s.trim())
        .filter(Boolean)

      // create() derives status (triage flag → 'triage', else 'ready'); move to
      // the requested column when they differ, so a per-column add lands right.
      // `writeBoard` pins BOTH writes to the board the user picked; it is
      // `undefined` outside All Boards mode, where `$boardSlug` still decides.
      const { task, warning } = await createTask(
        {
          assignee: assignee === PARKED ? undefined : assignee || resolvedDefault,
          body: bodyText.trim() || undefined,
          goal_mode: goalMode,
          parents: parent ? [parent] : undefined,
          // Images travel exclusively as staged tokens, never inlined into
          // `body` — the backend promotes each token into a real attachment.
          pending_attachment_tokens: pendingImages.length ? pendingImages.map(image => image.token) : undefined,
          priority,
          skills: skillList.length ? skillList : undefined,
          title: trimmed,
          triage: isTriage,
          workspace_kind: workspaceKind,
          ...overrideCreateFields(modelOverride),
          // Empty → backend inherits the board's default project dir.
          workspace_path: workspaceKind !== 'scratch' && workspacePath.trim() ? workspacePath.trim() : undefined
        },
        writeBoard
      )

      if (task && task.status !== target) {
        await patchTask(task.id, { status: target }, writeBoard)
      }

      // Dispatcher-presence warning ("this ready task will sit idle") — not an
      // error, but the user should know.
      if (warning) {
        host.notify({ kind: 'warning', message: warning })
      }

      await qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      // Submitted images are now real attachments — clear without re-deleting
      // the (already-promoted) staged blobs.
      setPendingImages([])
      onClose()
    } catch (err) {
      setError(errText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && handleClose()} open={Boolean(target)}>
      {/* `overflow-visible`: DialogContent publishes ITSELF as the portal
          container for popovers opened inside it (dialog-portal-context), and
          its default `overflow-y-auto` then crops them at the dialog's edge —
          the model menu below is born inside that scroll box. This dialog
          already owns a scroller on its body div, so the shell's clip is
          redundant here and dropping it is safe. The general fix to
          DialogContent is in flight as #75600; when that lands this override
          becomes a no-op and can go. */}
      <DialogContent className="w-[min(42rem,94vw)] max-w-none overflow-visible">
        <DialogHeader>
          <DialogTitle>{target ? k.newTaskIn(columnLabel(k, target)) : k.newTask}</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-[min(72vh,44rem)] flex-col gap-3 overflow-y-auto pr-0.5">
          {/* All Boards mode has no implied board — ask, defaulted to the
              server's current one, rather than letting the create resolve
              silently to whatever board happens to be active. */}
          {isAllBoards && (
            <Field label={k.board}>
              <Select onValueChange={setTargetBoard} value={targetBoard}>
                <SelectTrigger>
                  <SelectValue placeholder={k.pickBoard} />
                </SelectTrigger>
                <SelectContent>
                  {(boards?.boards ?? []).map(option => (
                    <SelectItem key={option.slug} value={option.slug}>
                      {option.name || option.slug}
                      {option.slug === serverCurrent ? k.boardDefaultSuffix : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.pickBoardHint}</span>
            </Field>
          )}
          <Input
            autoFocus
            onChange={event => setTitle(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              }
            }}
            placeholder={isTriage ? k.titlePlaceholderTriage : k.titlePlaceholder}
            value={title}
          />
          <Textarea
            className="min-h-20"
            onChange={event => setBodyText(event.target.value)}
            onPaste={handlePaste}
            placeholder={k.descPlaceholder}
            value={bodyText}
          />

          {(pendingImages.length > 0 || uploadingImages > 0) && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                {k.pastedImages(pendingImages.length + uploadingImages)}
              </span>
              <div className="flex flex-wrap gap-2">
                {pendingImages.map(image => (
                  <div
                    className="group relative h-16 w-16 overflow-hidden rounded-md border border-(--ui-border)"
                    key={image.token}
                  >
                    <img alt={image.filename} className="h-full w-full object-cover" src={image.previewUrl} />
                    <Button
                      aria-label={k.removeImage}
                      className="absolute top-0.5 right-0.5 h-4 w-4 opacity-0 group-hover:opacity-100"
                      onClick={() => removePendingImage(image.token)}
                      size="icon-xs"
                      variant="destructive"
                    >
                      <Codicon name="close" size="0.6rem" />
                    </Button>
                  </div>
                ))}
                {Array.from({ length: uploadingImages }).map((_, index) => (
                  <div
                    className="flex h-16 w-16 items-center justify-center rounded-md border border-(--ui-border) border-dashed"
                    key={`uploading-${index}`}
                  >
                    <Codicon name="loading" size="1rem" spinning />
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <Field label={k.priority}>
              <PriorityPicker onChange={setPriority} priority={priority} />
            </Field>
            <Field label={k.workspace}>
              <Select onValueChange={setWorkspaceKind} value={workspaceKind}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WORKSPACE_KINDS.map(kind => (
                    <SelectItem key={kind} value={kind}>
                      {kind}
                      {kind === boardDefaultKind ? k.boardDefaultSuffix : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          {workspaceKind !== 'scratch' && (
            <Field label={k.workspaceOverride}>
              <Input
                onChange={event => setWorkspacePath(event.target.value)}
                placeholder={boardDefaultDir || k.workspaceInherit}
                value={workspacePath}
              />
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                {boardDefaultDir ? k.workspaceInheritDir(boardDefaultDir) : k.workspaceInheritGeneric}
              </span>
            </Field>
          )}

          <Field label={k.assignee}>
            <Select onValueChange={v => setAssignee(v === NO_PARENT ? '' : v)} value={assignee || NO_PARENT}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PARENT}>{k.defaultOption(resolvedDefault)}</SelectItem>
                {(roster?.profiles ?? [])
                  .filter(profile => profile.name !== resolvedDefault)
                  .map(profile => (
                    <SelectItem key={profile.name} value={profile.name}>
                      {profile.name}
                    </SelectItem>
                  ))}
                <SelectItem value={PARKED}>{k.parkedOption}</SelectItem>
              </SelectContent>
            </Select>
          </Field>

          <Field label={k.skills}>
            <Input onChange={event => setSkills(event.target.value)} placeholder={k.skillsPlaceholder} value={skills} />
          </Field>

          <Field label={k.model}>
            <ModelOverrideField onChange={setModelOverride} value={modelOverride} />
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.modelHint}</span>
          </Field>

          {parentOptions.length > 0 && (
            <Field label={k.parent}>
              <Select onValueChange={v => setParent(v === NO_PARENT ? '' : v)} value={parent || NO_PARENT}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_PARENT}>{k.noParent}</SelectItem>
                  {parentOptions.map(option => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.title || option.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          )}

          <label className="flex cursor-pointer items-center gap-2 text-[0.75rem] text-(--ui-text-secondary)">
            <Switch aria-label={k.goalMode} checked={goalMode} onCheckedChange={setGoalMode} size="xs" />
            {k.goalMode}
          </label>

          {error && <span className="text-[0.75rem] text-destructive">{error}</span>}
        </div>
        <DialogFooter>
          <div className="mr-auto flex items-center gap-1 text-[0.75rem] text-(--ui-text-tertiary)">
            {estimate?.ok ? (
              <>
                <Tip label={estimate.rationale || k.roughEstimate}>
                  <span className="font-medium tabular-nums text-(--ui-text-secondary)">
                    ~{compactNumber(estimate.est_tokens)} {k.tokUnit}
                    {estimate.complexity ? ` · ${k.complexity[estimate.complexity] ?? estimate.complexity}` : ''}
                  </span>
                </Tip>
                <Tip label={k.reEstimate}>
                  <Button
                    aria-label={k.reEstimate}
                    disabled={!title.trim() || estMut.isPending}
                    onClick={() => estMut.mutate()}
                    size="icon-xs"
                    variant="ghost"
                  >
                    <Codicon name="refresh" size="0.7rem" spinning={estMut.isPending} />
                  </Button>
                </Tip>
              </>
            ) : (
              <Tip label={k.estimateTip}>
                <Button
                  disabled={!title.trim() || estMut.isPending}
                  onClick={() => estMut.mutate()}
                  size="xs"
                  variant="ghost"
                >
                  <Codicon
                    name={estMut.isPending ? 'loading' : 'dashboard'}
                    size="0.75rem"
                    spinning={estMut.isPending}
                  />
                  {estMut.isPending ? k.estimating : k.estimate}
                </Button>
              </Tip>
            )}
          </div>
          <Button onClick={handleClose} variant="text">
            {k.cancel}
          </Button>
          <Button
            disabled={!title.trim() || busy || uploadingImages > 0 || (isAllBoards && !targetBoard)}
            onClick={() => void submit()}
          >
            {busy ? k.creating : k.createTask}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── idea capture (Phase 2.15) ───────────────────────────────────────────────

/**
 * Free-typed roadmap idea capture — jot a rough idea straight from the board
 * into a card in the board's `idea` lane, without opening an editor or
 * filing a premature card. A rejected/unavailable roadmap is reported
 * distinctly from success (`k.ideaUnavailable` vs. `k.ideaSaved`) per the
 * card's acceptance criteria. On success this invalidates the board query
 * prefix so the new card shows up immediately, including in All Boards mode.
 */
export function IdeaCaptureDialog({ onClose, open }: { onClose: () => void; open: boolean }) {
  const k = useKanban()
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    if (open) {
      setText('')
      setBusy(false)
      setError(null)
    }
  }, [open])

  const submit = async () => {
    const trimmed = text.trim()

    if (!trimmed || busy) {
      return
    }

    setBusy(true)
    setError(null)

    try {
      const { ok, reason } = await addRoadmapIdea(trimmed)

      if (ok) {
        host.notify({ kind: 'success', message: k.ideaSaved })
        void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
        onClose()
      } else {
        // Distinct from a thrown error: the request succeeded, the idea card
        // creation did not (missing/unavailable roadmap lane, empty after
        // sanitization) — surface it inline so the user can decide whether
        // to retry rather than silently losing the idea.
        setError(reason === 'empty_idea' ? k.ideaEmpty : k.ideaUnavailable)
        setBusy(false)
      }
    } catch (err) {
      setError(errText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog onOpenChange={next => !next && onClose()} open={open}>
      <DialogContent className="w-[min(28rem,94vw)]">
        <DialogHeader>
          <DialogTitle>{k.ideaTitle}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <p className="text-xs text-(--ui-text-tertiary)">{k.ideaHint}</p>
          <Textarea
            autoFocus
            className="min-h-24"
            maxLength={300}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                void submit()
              }
            }}
            placeholder={k.ideaPlaceholder}
            value={text}
          />
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button onClick={onClose} size="sm" variant="ghost">
            {k.cancel}
          </Button>
          <Button disabled={!text.trim() || busy} onClick={() => void submit()} size="sm">
            {busy ? k.ideaSaving : k.ideaSave}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
