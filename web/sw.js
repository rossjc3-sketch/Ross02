const CACHE='quickcut-shell-v3';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/style.css','/app.js','/timeline.js','/icon.svg','/manifest.webmanifest']))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET'||new URL(e.request.url).origin!==location.origin||/\/(api|media)\//.test(e.request.url))return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))});
