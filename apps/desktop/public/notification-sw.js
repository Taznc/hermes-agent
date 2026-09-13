// notification-sw.js — SPIKE: minimal Service Worker so web-desktop OS
// notifications can carry ACTION BUTTONS (Approve/Reject, etc).
//
// The plain `new Notification()` constructor has no `actions` support in any
// browser — action buttons only exist on notifications shown via a Service
// Worker's `registration.showNotification()`. This worker does nothing but
// relay `notificationclick` back to the page(s) that are open; all the
// actual branching logic (which callback to fire) lives in
// web-bridge-shim.ts on the page side, so this file stays a dumb transport
// and never needs to know about Hermes' notification kinds.
//
// Registered by web-bridge-shim.ts's ensureNotificationServiceWorker().
// Scope is the app root ('/'), matching where it's served from.

self.addEventListener('install', () => {
  // Activate immediately — no cached assets to warm, and a stale worker
  // from a previous version should not keep intercepting clicks.
  self.skipWaiting()
})

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('notificationclick', event => {
  const notification = event.notification
  const actionId = event.action || undefined
  const data = notification.data || {}

  notification.close()

  event.waitUntil(
    (async () => {
      const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const message = { source: 'hermes-notification-sw', actionId, data }

      if (allClients.length > 0) {
        for (const client of allClients) {
          client.postMessage(message)
        }

        // Bring the app to the foreground — mirrors Electron's focusWindow()
        // in the equivalent 'hermes:notify' click/action handlers.
        await allClients[0].focus()
      } else if (self.registration.scope) {
        await self.clients.openWindow(self.registration.scope)
      }
    })()
  )
})
