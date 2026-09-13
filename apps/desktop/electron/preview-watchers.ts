// Registry for main-process file/directory watchers backing the preview pane
// and the disk-plugin door (runtime-loader's watchDirectory IPC). Pulled out
// of main.ts so the ownership/cleanup logic is unit-testable without an
// Electron runtime.
//
// Two invariants this exists to protect:
//  - A watcher's 'error' listener is mandatory. fs.watch() returns an
//    EventEmitter; an unhandled 'error' throws and crashes the whole
//    Electron main process. On Windows, deleting/renaming a watched
//    directory raises EPERM on the next tick — reachable any time a user
//    deletes a plugin folder or the file a preview happens to be open on.
//  - Every watcher is owned by the webContents that requested it. Nothing
//    upstream tied fs.watch handles + debounce timers to a renderer's
//    lifetime, so a crash/reload/re-home orphaned them forever (inotify
//    watches are finite on Linux; this degrades multi-day sessions).

export interface PreviewWatcherHandle {
  close(): void
}

interface PreviewWatcherEntry extends PreviewWatcherHandle {
  webContentsId: number | null
}

export class PreviewWatcherRegistry {
  private readonly entries = new Map<string, PreviewWatcherEntry>()

  get size(): number {
    return this.entries.size
  }

  has(id: string): boolean {
    return this.entries.has(id)
  }

  keys(): IterableIterator<string> {
    return this.entries.keys()
  }

  register(id: string, handle: PreviewWatcherHandle, webContentsId: number | null = null): void {
    this.entries.set(id, { ...handle, webContentsId })
  }

  /** Closes and forgets one watcher. Idempotent — an explicit stop IPC racing
   *  an error-path (or owner-cleanup) close is a harmless no-op the second
   *  time around. */
  stop(id: string): boolean {
    const entry = this.entries.get(id)

    if (!entry) {
      return false
    }

    this.entries.delete(id)

    try {
      entry.close()
    } catch {
      // Already closed/torn down — stop() must stay safe to call twice.
    }

    return true
  }

  stopAll(): void {
    for (const id of [...this.entries.keys()]) {
      this.stop(id)
    }
  }

  /** Renderer crash/reload/re-home: close every watcher it opened so the
   *  fs.watch handles and debounce timers don't outlive the page that asked
   *  for them. */
  stopForWebContents(webContentsId: number): void {
    for (const [id, entry] of [...this.entries.entries()]) {
      if (entry.webContentsId === webContentsId) {
        this.stop(id)
      }
    }
  }
}

/** Attach the mandatory 'error' handler to a raw fs.FSWatcher (or anything
 *  matching its minimal EventEmitter shape). Without this, an EPERM/ENOENT
 *  raised when a watched path is deleted/renamed while watched is an
 *  unhandled 'error' on an EventEmitter — which throws and kills the
 *  Electron main process. `onError` should at minimum forget the watcher
 *  (registry.stop(id)); callers doing debounced work should also clear
 *  their timer there. */
export function guardWatcherErrors(
  watcher: { on(event: 'error', listener: (error: unknown) => void): unknown; close(): void },
  onError: (error: unknown) => void
): void {
  watcher.on('error', error => {
    try {
      watcher.close()
    } catch {
      // Already gone — the point is onError still runs so state stays clean.
    }

    onError(error)
  })
}
