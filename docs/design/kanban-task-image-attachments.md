# Design: pasted-image attachments for new Kanban tasks

Status: draft spec for implementers (t_31ee24d1 frontend, t_d61c0d40 backend)
Related: t_149f7c2e (root request), t_cae4c2ba (render images in task view)
Author: t_6e54bc44

## 0. Context / what already exists

The kanban board already has a **generic file-attachment system** — do not
build a parallel one. It lives in `hermes_cli/kanban_db.py` +
`plugins/kanban/dashboard/plugin_api.py`:

- Table `task_attachments` (id, task_id, filename, stored_path, content_type,
  size, uploaded_by, created_at). One row per file, blob on disk under
  `attachments_root(board)/<task_id>/<stored_name>`.
- `kanban_db.store_attachment_bytes(conn, task_id, filename, data,
  content_type=..., uploaded_by=..., board=..., max_bytes=...)` — the single
  write path shared by the dashboard upload endpoint, the agent's
  `kanban_attach` tool, and `hermes kanban attach`. Validates size
  (`KANBAN_ATTACHMENT_MAX_BYTES = 25 MiB`), sanitises the filename
  (`_safe_attachment_name`), resolves name collisions
  (`_collision_free_path`), writes the file, inserts the row, appends an
  `attached` event. Raises `AttachmentTooLarge` / `ValueError`.
- REST surface: `GET/POST /tasks/{id}/attachments`, `GET/DELETE
  /attachments/{id}` (`plugin_api.py` lines ~701-824). `POST` is
  `multipart/form-data` (`file`, optional `uploaded_by` form field).

**The one gap**: every existing path requires the task to already exist
(`add_attachment` raises `ValueError: unknown task <id>` otherwise). The "new
task" dialog needs to accept pasted images *before* the user hits Create —
t_31ee24d1's acceptance criteria explicitly want the image uploaded and
previewed as soon as it's pasted, with removal supported pre-submit. That is
the only real design problem to solve here; everything else is "point pasted
images at the attachment system that's already there."

## 1. Decision: reuse `task_attachments`, add a small staging table

Do **not** add an `images` column/table on `tasks`, and do **not** create a
separate "task images" table parallel to `task_attachments`. An image pasted
into a task is just an attachment whose `content_type` starts with `image/`;
the UI distinguishes "images" from "files" purely by that prefix at render
time (t_cae4c2ba). This keeps one storage/query path for the drawer, the
worker's `build_worker_context`, and the CLI.

The only schema addition is a **staging table** for the pre-submit paste
window, since a `task_id` isn't available yet:

```sql
-- Pasted images (or any file) uploaded before the owning task exists, e.g.
-- from the "new task" dialog. Promoted into task_attachments (and this row
-- deleted) when the task is created; swept by a TTL reaper if the dialog is
-- abandoned. Lives under attachments_root(board)/_staged/<token>/.
CREATE TABLE IF NOT EXISTS staged_attachments (
    token        TEXT PRIMARY KEY,   -- opaque random id, client-facing
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    board        TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_staged_created ON staged_attachments(created_at);
```

Rationale for a separate table rather than nullable `task_attachments.task_id`:
`add_attachment`/`list_attachments`/the drawer/`build_worker_context` all
assume a real task id; making `task_id` nullable would force every one of
those call sites to filter it out. A distinct table with a short TTL keeps
the "real" attachments table's invariant (`task_id` always references a row
in `tasks`) intact, matches the existing pattern of one small purpose-built
table per concern (`task_runs`, `task_events`, `kanban_notify_subs`), and its
reaper is a two-line addition to the existing GC pass alongside
`reconcile_orphaned_running`.

### New constants (`hermes_cli/kanban_db.py`, next to `KANBAN_ATTACHMENT_MAX_BYTES`)

```python
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024        # existing, unchanged — generic files
KANBAN_IMAGE_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024  # new — pasted-image specific cap
KANBAN_IMAGE_ALLOWED_MIME_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})
KANBAN_STAGED_ATTACHMENT_TTL_SECONDS = 24 * 60 * 60   # abandoned-draft GC window
KANBAN_MAX_PENDING_ATTACHMENTS_PER_TASK = 10          # abuse guard on promote
```

## 2. Backend functions (`hermes_cli/kanban_db.py`)

```python
def stage_attachment_bytes(
    filename: str,
    data: bytes,
    *,
    content_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    board: Optional[str] = None,
) -> "StagedAttachment":
    """Validate (image mime allowlist + KANBAN_IMAGE_ATTACHMENT_MAX_BYTES),
    write the blob under attachments_root(board)/_staged/<token>/<safe_name>,
    insert a staged_attachments row, return it (includes the new `token`).
    Raises ValueError for bad filename/mime, AttachmentTooLarge for size.
    """

def get_staged_attachment(token: str, *, board: Optional[str] = None) -> Optional["StagedAttachment"]: ...

def delete_staged_attachment(token: str, *, board: Optional[str] = None) -> bool:
    """Remove the row + blob. Used both by the explicit DELETE endpoint
    (user removes a pasted image before submit) and by promotion cleanup."""

def promote_staged_attachments(
    conn: sqlite3.Connection,
    task_id: str,
    tokens: list[str],
    *,
    uploaded_by: Optional[str] = None,
    board: Optional[str] = None,
) -> tuple[list[Attachment], list[str]]:
    """For each token: look up the staged row, os.rename() the blob into
    task_attachments_dir(task_id) (same filesystem, so this is a metadata-only
    move, not a copy), insert the task_attachments row via add_attachment(),
    delete the staged row. Returns (promoted_attachments, warnings) — an
    unknown/expired/already-promoted token is a warning, never a failure of
    the whole task-creation call. Caps at KANBAN_MAX_PENDING_ATTACHMENTS_PER_TASK
    (extra tokens become warnings, not silently dropped)."""

def reap_staged_attachments(max_age_seconds: int = KANBAN_STAGED_ATTACHMENT_TTL_SECONDS) -> int:
    """Delete staged rows/blobs older than max_age_seconds (abandoned 'new
    task' dialogs). Call from the same dispatcher-tick GC pass that already
    runs reconcile_orphaned_running / release_stale_claims. Returns count
    removed."""
```

`StagedAttachment` dataclass mirrors `Attachment` minus `task_id`, plus
`token`.

## 3. REST API contract (`plugins/kanban/dashboard/plugin_api.py`)

### 3.1 `POST /attachments/staged` — upload a pasted image pre-submit

```
POST /attachments/staged?board=<slug>
Content-Type: multipart/form-data
  file: <binary>                 (required)
  uploaded_by: <string>          (optional form field, default "dashboard")

201 Created
{
  "attachment": {
    "token": "st_9f2a1c4e",
    "filename": "screenshot.png",
    "content_type": "image/png",
    "size": 48213,
    "created_at": 1787800000
  }
}

400 Bad Request   {"detail": "unsupported image type: application/pdf; accepted: image/png, image/jpeg, image/gif, image/webp"}
413 Payload Too Large {"detail": "attachment exceeds 10 MB limit"}
```

Mirrors the existing `upload_task_attachment` handler's streaming-with-cap
pattern (read in 1 MiB chunks, abort + unlink past the cap) but validates
`file.content_type` against `KANBAN_IMAGE_ALLOWED_MIME_TYPES` up front and
checks the sniffed size against `KANBAN_IMAGE_ATTACHMENT_MAX_BYTES` instead of
the generic 25 MiB cap. This endpoint is deliberately image-only; the
existing `/tasks/{id}/attachments` endpoint stays generic (PDFs, source docs,
25 MiB) and untouched.

### 3.2 `DELETE /attachments/staged/{token}` — remove before submit

```
DELETE /attachments/staged/{token}?board=<slug>
200 OK  {"ok": true, "token": "st_9f2a1c4e"}
404 Not Found  {"detail": "staged attachment not found"}
```

### 3.3 `GET /attachments/staged/{token}` — serve the staged blob (optional, parity with `GET /attachments/{id}`)

```
GET /attachments/staged/{token}?board=<slug>
200 OK  <image bytes, Content-Type from stored content_type>
404 Not Found
```

Not required for the paste-preview itself (the browser already has the
`File`/`Blob` from the clipboard event and can `URL.createObjectURL` it
locally), but keeps parity with the download route and covers a dialog
reload / multi-tab edge case.

### 3.4 `POST /tasks` — extend `CreateTaskBody` with pending tokens

Add one field to the existing Pydantic model in `plugin_api.py`:

```python
class CreateTaskBody(BaseModel):
    ...  # unchanged existing fields
    pending_attachment_tokens: list[str] = Field(default_factory=list)
```

Handler change: after `kanban_db.create_task(...)` returns `task_id`, call
`promote_staged_attachments(conn, task_id, payload.pending_attachment_tokens,
uploaded_by="dashboard", board=board)` before building the response. Response
shape gains an `attachments` array (same `_attachment_dict` serializer the
drawer already uses) and an optional `attachment_warnings` array so a stale
token never fails task creation:

```
POST /tasks?board=<slug>
{
  "title": "Fix the login bug",
  "body": "Repro steps...",
  "pending_attachment_tokens": ["st_9f2a1c4e", "st_1b77e0aa"],
  ... (all existing CreateTaskBody fields, unchanged)
}

200 OK
{
  "task": { ...existing task fields... },
  "attachments": [
    {"id": 42, "task_id": "t_...", "filename": "screenshot.png",
     "content_type": "image/png", "size": 48213, "uploaded_by": "dashboard",
     "stored_path": "/abs/path", "created_at": 1787800000}
  ],
  "attachment_warnings": [],       // e.g. ["token st_stale not found or expired"]
  "warning": null                  // existing dispatcher-presence warning, unchanged
}
```

No breaking change: `pending_attachment_tokens` defaults to `[]`, existing
callers (CLI, agent tool, older dashboard builds) are unaffected, and
`attachments`/`attachment_warnings` are additive response fields.

## 4. Frontend flow (for t_31ee24d1)

1. User opens `NewTaskDialog` (`apps/desktop/src/plugins/kanban/board.tsx`).
   Add local state `const [pendingImages, setPendingImages] = useState<PendingImage[]>([])`
   where `PendingImage = { token: string; filename: string; previewUrl: string; size: number }`.
2. Attach a `onPaste` handler to the description `Textarea` (and/or a drop
   zone): read `event.clipboardData.items`, filter `item.type.startsWith('image/')`,
   for each: `const blob = item.getAsFile()`, immediately
   `POST /attachments/staged` with the blob, and on success push
   `{ token, filename, previewUrl: URL.createObjectURL(blob), size }` into
   `pendingImages`. Show a thumbnail strip below the textarea with a remove
   (×) button per thumbnail.
3. Remove button: call `DELETE /attachments/staged/{token}`, drop it from
   `pendingImages`, `URL.revokeObjectURL(previewUrl)`.
4. On submit: include `pending_attachment_tokens: pendingImages.map(p => p.token)`
   in the `createTask(...)` call. On dialog close/cancel without submitting,
   best-effort `DELETE` any still-pending tokens (not required for
   correctness — the TTL reaper cleans them up regardless — but keeps the
   board tidy immediately).
5. Never inline the image into `bodyText` as markdown/base64 — the field
   stays plain text; images travel exclusively via `pending_attachment_tokens`
   → `attachments`.

## 5. Rendering (for t_cae4c2ba)

`GET /tasks/{id}` already returns `"attachments": [...]` (unchanged). The
detail view filters `attachments.filter(a => a.content_type?.startsWith('image/'))`
for an image strip (click to enlarge via existing `Dialog`/lightbox pattern),
and lists the rest under "Files" as today. Card thumbnail (optional per the
task body) uses the first image attachment's `GET /attachments/{id}` URL.
Broken/missing image → `onError` on the `<img>` falls back to a placeholder
icon, never throws.

## 6. Validation rules summary

| Rule | Value | Enforced where |
|---|---|---|
| Accepted mime types (pasted images) | `image/png`, `image/jpeg`, `image/gif`, `image/webp` | `stage_attachment_bytes` (400 on mismatch) |
| Max size per pasted image | 10 MiB | `stage_attachment_bytes` (413 on exceed, streamed-abort like the existing handler) |
| Max pending images per task | 10 | `promote_staged_attachments` (excess tokens → warnings) |
| Staged blob TTL if never promoted | 24h | `reap_staged_attachments`, run from the existing dispatcher GC tick |
| Generic (non-image) task attachments | unchanged: any mime, 25 MiB, `task_attachments` table | existing `/tasks/{id}/attachments` |

## 7. Feasibility check against existing storage

- No migration risk: one new table (`staged_attachments`), zero changes to
  `tasks` or `task_attachments` columns/indexes.
- Filesystem layout stays consistent with `attachments_root(board)` (already
  per-board partitioned); staged blobs live in a `_staged/` subdirectory of
  the same root so board-scoping and the existing "confirm the blob is under
  the board's attachments root before serving" defense-in-depth check in
  `download_attachment` needs no change (add the analogous check to the new
  staged download route).
- Promotion is a filesystem rename, not a copy — O(1) regardless of image
  size, no double disk usage during the create-task request.
- Reuses `_safe_attachment_name`, `_collision_free_path`,
  `AttachmentTooLarge`, and the `attached` event kind as-is — no drift
  between the three existing surfaces (dashboard, agent tool, CLI) and this
  new one.
- GC reuses the existing dispatcher-tick GC pass rather than introducing a
  new background job.

## 8. Open items for implementers (not blocking, note in completion metadata)

- Desktop/back-compat: an older dashboard build that doesn't send
  `pending_attachment_tokens` continues to work unchanged (field defaults to
  `[]`) — no fallback branch needed beyond that.
- `hermes kanban attach` CLI and the agent's `kanban_attach` tool are
  untouched by this change; they already require an existing `task_id` and
  that remains their contract (this spec only adds the pre-task-creation
  staging path used by the dashboard/desktop "new task" dialog).
- Platform impact: none — pure backend/DB + web (dashboard/desktop) UI,
  no OS-conditional code paths.
