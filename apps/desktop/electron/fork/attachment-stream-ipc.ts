// Fork-owned: chunked attachment-read IPC registration.
//
// Chunked attachment transport: the renderer drives repeated calls at
// increasing `offset` and concatenates the returned base64 strings itself,
// so at no point does main hold the whole file (or its base64 expansion) in
// one Buffer/string, and no single IPC reply exceeds ATTACHMENT_CHUNK_BYTES
// (~8 MiB raw, ~11 MiB base64) regardless of how large the source file is.
// Same total-size cap and path hardening as the whole-file reader
// (hermes:readFileDataUrlForAttach, kept upstream-side for older renderer
// bundles); only the transport shape changes.
//
// Path authorization stays injected by the anchor (resolveRequestedPath is
// main.ts's hardened resolver); the bounded read/framing implementation is
// ../hardening's readFileChunkForIpc.

import { ATTACHMENT_CHUNK_BYTES, ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES, readFileChunkForIpc } from '../hardening'

export interface AttachmentStreamIpcDeps {
  ipcMain: { handle(channel: string, listener: (...args: any[]) => unknown): void }
  /** Hardened path authorization — throws for paths the renderer may not read. */
  resolveRequestedPath(filePath: string, options: { purpose: string }): string
  mimeTypeForPath(filePath: string): string
}

export function registerAttachmentStreamIpc(deps: AttachmentStreamIpcDeps): void {
  deps.ipcMain.handle('hermes:readFileChunkForAttach', async (_event, filePath, offset) => {
    return readFileChunkForIpc(
      filePath,
      {
        maxBytes: ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES,
        mimeType: deps.mimeTypeForPath(deps.resolveRequestedPath(filePath, { purpose: 'Attachment upload' })),
        purpose: 'Attachment upload'
      },
      Number(offset) || 0,
      ATTACHMENT_CHUNK_BYTES
    )
  })
}
