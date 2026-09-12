/* Ribbon service worker: offline app shell, cached fonts and tag reader, share-target inbox. */
const VERSION = 'ribbon-v1';
const SHELL = VERSION + '-shell';
const RUNTIME = VERSION + '-runtime';
const INBOX = 'ribbon-inbox';

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k.startsWith('ribbon-v') && !k.startsWith(VERSION)).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method === 'POST' && url.origin === location.origin && url.pathname.endsWith('/share')) {
    e.respondWith(receiveShare(req));
    return;
  }
  if (req.method !== 'GET') return;
  if (req.mode === 'navigate') { e.respondWith(networkFirst(req, SHELL)); return; }
  if (url.origin === location.origin) { e.respondWith(staleWhileRevalidate(req, SHELL)); return; }
  if (/(^|\.)(fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.jsdelivr\.net)$/.test(url.host)) {
    e.respondWith(cacheFirst(req, RUNTIME));
  }
});

async function receiveShare(req) {
  try {
    const fd = await req.formData();
    const files = fd.getAll('audio').filter(f => f && typeof f === 'object' && 'size' in f && f.size > 0);
    const cache = await caches.open(INBOX);
    let n = Date.now();
    for (const f of files) {
      const key = new Request(location.origin + '/__inbox/' + (n++));
      const headers = { 'X-Name': encodeURIComponent(f.name || 'shared-audio'), 'Content-Type': f.type || 'application/octet-stream', 'Content-Length': String(f.size) };
      await cache.put(key, new Response(f, { headers }));
    }
  } catch (err) {
    console.warn('share receive failed', err);
  }
  return Response.redirect(self.registration.scope + '?shared=1', 303);
}

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  } catch {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    const keys = await cache.keys();
    const nav = keys.find(k => new URL(k.url).pathname === new URL(self.registration.scope).pathname || k.mode === 'navigate');
    if (nav) return cache.match(nav);
    return new Response('<h1>Offline</h1><p>Ribbon has not been opened online yet on this device.</p>', { status: 503, headers: { 'Content-Type': 'text/html' } });
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  const refresh = fetch(req).then(res => { if (res && res.ok) cache.put(req, res.clone()); return res; }).catch(() => null);
  return hit || (await refresh) || new Response('', { status: 504 });
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone());
    return res;
  } catch {
    return new Response('', { status: 504 });
  }
}
