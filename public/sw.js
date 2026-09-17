'use strict';
/* 离线壳缓存（仅 localhost / HTTPS 环境生效；普通局域网 HTTP 会被浏览器忽略，不影响使用） */
const CACHE = 'aiwechat-v20260827-offline-keeps-v1';
const SHELL = [
  '/',
  '/index.html',
  '/css/style.css',
  '/js/personas.js',
  '/js/store.js',
  '/js/app.js',
  '/js/chatlist.js',
  '/js/contacts.js',
  '/js/chat.js',
  '/js/chat_stream.js',
  '/js/voice_call.js',
  '/js/incoming_call.js',
  '/js/companion.js',
  '/js/settings.js',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match('/index.html')))
  );
});
