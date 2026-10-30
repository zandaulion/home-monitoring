// Adapted from the local pwa-kit reference worker. Only the empty shell is
// persisted: authenticated APIs, images and invitation codes are network-only.
const CACHE_NAME = 'home-monitor-__BUILD_VERSION__';
const SHELL = ['/', '/assets/app.js', '/assets/timeline.js', '/assets/style.css', '/pwa-update.js',
  '/manifest.webmanifest', '/icons/icon.svg', '/icons/icon-192.png',
  '/icons/icon-512.png', '/icons/icon-maskable.png', '/icons/apple-touch-icon.png'];
const STATIC_PATHS = new Set(SHELL);
importScripts('/sw-update.js');

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(SHELL.map(path => new Request(path, {cache:'reload'})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    await Promise.all((await caches.keys()).filter(key => key.startsWith('home-monitor-') && key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
    await announceUpdate();
  })());
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== 'GET') return;
  if (url.search || !STATIC_PATHS.has(url.pathname)) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(event.request, {cache:'no-cache'});
      if (response.ok) {
        const copy = response.clone();
        event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.put(url.pathname, copy)));
      }
      return response;
    } catch {
      return await caches.match(url.pathname) || new Response('Offline', {status:503});
    }
  })());
});

self.addEventListener('push', event => {
  let payload;
  try { payload = event.data?.json(); } catch {}
  if (!payload || (payload.expires_at && Date.now()/1000 > payload.expires_at)) return;
  // Use the encrypted payload directly. No origin request, face image, or
  // Tailscale connection is needed merely to display a notification.
  event.waitUntil(self.registration.showNotification(payload.title || 'Home Monitor', {
    body: payload.body || 'New activity is ready to review.',
    tag: payload.tag || 'home-monitor-activity'
  }));
});

// Clicking an alert opens private activity; it never bypasses device access.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type:'window', includeUncontrolled:true});
    const existing = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (existing) {
      existing.postMessage({type:'open-activity'});
      await existing.focus();
    } else await self.clients.openWindow('/#events');
  })());
});
