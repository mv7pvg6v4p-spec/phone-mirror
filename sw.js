/* Service worker so dwell alerts still appear when the mirror tab is in the
   background - Chrome ignores `new Notification()` from hidden tabs. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then(list => {
      for (const c of list) {
        if ('focus' in c) return c.focus();
      }
      return self.clients.openWindow('/');
    }));
});

self.addEventListener('push', e => {
  const d = (() => { try { return e.data.json(); } catch (_) { return {}; } })();
  e.waitUntil(self.registration.showNotification(d.title || 'Phone Mirror', {
    body: d.body || '', icon: d.icon || '/frame.jpg', tag: d.tag || 'phone-mirror',
  }));
});
