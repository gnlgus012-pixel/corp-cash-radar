// 법인WM AI레이더 — 앱 설치용 최소 서비스워커
// 화면(index.html)은 항상 최신을 먼저 받고, 연결이 안 될 때만 마지막으로 받은 화면을 보여준다.
// 데이터(data.enc 등)는 건드리지 않음 → 매일 갱신 데이터가 그대로 반영됨.
const CACHE = "radar-shell-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone(); caches.open(CACHE).then(c => c.put("./index.html", copy)); return r;
  }).catch(() => caches.match("./index.html")));
});
