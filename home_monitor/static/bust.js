// The pwa-kit escape hatch, externalised to respect this app's strict CSP.
(async () => {
  try {
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(keys.filter(key => key.startsWith('home-monitor-')).map(key => caches.delete(key)));
    }
    if ('serviceWorker' in navigator) {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map(registration => registration.unregister()));
    }
  } finally {
    sessionStorage.setItem('pwa-kit:updated', '1');
    location.replace('/');
  }
})();
