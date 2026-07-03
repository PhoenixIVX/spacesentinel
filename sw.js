const CACHE = 'sentinel-v1';
const FONT_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function cacheFirst(request) {
  return caches.open(CACHE).then(c =>
    c.match(request).then(hit => hit || fetch(request).then(res => {
      if (res.ok) c.put(request, res.clone());
      return res;
    }))
  );
}
function networkFirst(request) {
  return caches.open(CACHE).then(c =>
    fetch(request).then(res => {
      if (res.ok) c.put(request, res.clone());
      return res;
    }).catch(() => c.match(request))
  );
}

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin === location.origin) {
    e.respondWith(url.pathname.endsWith('.png') ? cacheFirst(e.request) : networkFirst(e.request));
  } else if (FONT_HOSTS.includes(url.hostname)) {
    e.respondWith(cacheFirst(e.request));
  }
});
