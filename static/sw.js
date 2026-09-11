const CACHE_NAME = "tawtheeq-v12";
const OFFLINE_URL = "/static/offline.html";
const CORE_ASSETS = [
  OFFLINE_URL,
  // No query string: the manifest is looked up by path, so a versioned key
  // here would never be matched by the request that needs it offline.
  "/static/manifest.json",
  "/static/img/pwa/icon-192.png",
  "/static/img/pwa/icon-512.png",
  "/static/img/pwa/icon-maskable-192.png",
  "/static/img/pwa/icon-maskable-512.png",
  "/static/img/pwa/apple-touch-icon-180.png",
  // The badge is drawn by the OS while the device may be offline.
  "/static/img/pwa/badge-96.png",
  "/static/img/pwa/badge-72.png",
  "/static/css/fonts.css",
  "/static/vendor/fonts/cairo/cairo-arabic.woff2"
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await Promise.allSettled(CORE_ASSETS.map(async (url) => {
      const response = await fetch(new Request(url, { cache: "reload" }));
      if (response.ok) await cache.put(url, response);
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)));
    if (self.registration.navigationPreload) {
      await self.registration.navigationPreload.enable();
    }
    await self.clients.claim();
  })());
});

function isSameOrigin(request) {
  return new URL(request.url).origin === self.location.origin;
}

function isStaticRequest(request) {
  return new URL(request.url).pathname.startsWith("/static/");
}

function isManifestRequest(request) {
  return new URL(request.url).pathname === "/static/manifest.json";
}

function isApiRequest(request) {
  return new URL(request.url).pathname.startsWith("/api/");
}

async function networkFirstManifest(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  } catch (error) {
    return (await cache.match(request)) || Response.error();
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  const network = fetch(request).then(async (response) => {
    if (response.ok) await cache.put(request, response.clone());
    return response;
  });

  if (cached) {
    network.catch(() => {});
    return cached;
  }

  return network;
}

async function handleNavigation(event) {
  try {
    const preload = await event.preloadResponse;
    if (preload) return preload;
    return await fetch(event.request);
  } catch (error) {
    const cached = await caches.match(OFFLINE_URL);
    return cached || new Response("لا يوجد اتصال بالإنترنت.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || !isSameOrigin(request)) return;

  // صفحات الحساب والتقارير شخصية؛ لا نخزّن HTML أو ردود API في الجهاز.
  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(event));
    return;
  }

  if (isApiRequest(request)) {
    event.respondWith(fetch(request).catch(() => new Response(
      JSON.stringify({ detail: "لا يوجد اتصال بالإنترنت." }),
      { status: 503, headers: { "Content-Type": "application/json; charset=utf-8" } }
    )));
    return;
  }

  if (isManifestRequest(request)) {
    event.respondWith(networkFirstManifest(request));
    return;
  }

  if (isStaticRequest(request)) {
    event.respondWith(staleWhileRevalidate(request));
  }
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("push", (event) => {
  event.waitUntil((async () => {
    let payload = {};
    try {
      payload = event.data ? event.data.json() : {};
    } catch (error) {
      payload = { body: event.data ? event.data.text() : "" };
    }

    const title = String(payload.title || "تنبيه جديد من منصة توثيق").slice(0, 100);
    const body = String(payload.body || "لديك تنبيه جديد. افتح التطبيق للاطلاع عليه.").slice(0, 240);
    const rawUrl = String(payload.url || "/notifications/mine/");
    let targetUrl = "/notifications/mine/";
    try {
      const parsed = new URL(rawUrl, self.location.origin);
      if (parsed.origin === self.location.origin) targetUrl = parsed.pathname + parsed.search + parsed.hash;
    } catch (error) {}

    await self.registration.showNotification(title, {
      body,
      icon: payload.icon || "/static/img/pwa/icon-192.png",
      badge: payload.badge || "/static/img/pwa/badge-96.png",
      tag: payload.tag || "tawtheeq-notification",
      renotify: true,
      requireInteraction: Boolean(payload.requireInteraction),
      dir: "rtl",
      lang: "ar",
      data: {
        url: targetUrl,
        notificationId: payload.notificationId || null
      },
      actions: [
        { action: "open", title: "فتح التنبيه" },
        { action: "dismiss", title: "إغلاق" }
      ]
    });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  if (event.action === "dismiss") return;

  event.waitUntil((async () => {
    const rawUrl = event.notification.data && event.notification.data.url;
    let target = new URL("/notifications/mine/", self.location.origin);
    try {
      const parsed = new URL(rawUrl || "/notifications/mine/", self.location.origin);
      if (parsed.origin === self.location.origin) target = parsed;
    } catch (error) {}

    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      try {
        if ("navigate" in client) await client.navigate(target.href);
        return await client.focus();
      } catch (error) {}
    }
    if (self.clients.openWindow) return self.clients.openWindow(target.href);
    return undefined;
  })());
});
