import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// Capture app. Served plain http on localhost:8022; a cloudflared tunnel
// terminates TLS so the camera (getUserMedia) and the service worker — both
// secure-context only — work on the device.
//
// Phase B.3 pins a STABLE hostname (Decision 5): a *named* cloudflared tunnel at
// capture.sportslinephotography.com → this Vite server on the office desktop. A
// home-screen install (and its IndexedDB queue) is bound to the origin, so the
// quick-tunnel's random host is no longer acceptable — it would orphan installs.
// The browser still only ever talks to this origin; /api is proxied to :8020
// server-side, so uploads stay same-origin (no CORS, no backend change).
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
const STABLE_HOST = 'capture.sportslinephotography.com';
const ALLOWED_HOSTS = [STABLE_HOST, 'localhost'];
const API_PROXY = { '/api': 'http://localhost:8020' };

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',     // a new build activates on the next launch
      injectRegister: false,          // we call registerSW() ourselves in main.jsx
      devOptions: { enabled: true },  // exercise the SW in `npm run dev` too…
      includeAssets: [
        'icons/icon-192.png',
        'icons/icon-512.png',
        'icons/icon-maskable-512.png',
      ],
      manifest: {
        name: 'Reference Capture — Player Sort',
        short_name: 'Reference Capture',
        description: 'Offline reference-photo capture for photo shoots.',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#0b0d12',
        theme_color: '#0b0d12',
        icons: [
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'icons/icon-maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        // SPA shell: serve the cached index.html for navigations so the app
        // opens with no connection…
        navigateFallback: '/index.html',
        // …but NEVER fall back (or otherwise serve from cache) for /api — those
        // must hit the network so a failure tells the app it's offline and to
        // queue. /api is fetch (not a navigation), so this is belt-and-suspenders.
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname.startsWith('/api'),
            handler: 'NetworkOnly',
          },
        ],
      },
    }),
  ],
  // …but the realistic SW/offline test target is the BUILD: `npm run build` then
  // `npm run preview` behind the tunnel (Section 1). `preview` mirrors `server`.
  server: {
    port: 8022,
    strictPort: true,
    proxy: API_PROXY,
    allowedHosts: ALLOWED_HOSTS,
  },
  preview: {
    port: 8022,
    strictPort: true,
    proxy: API_PROXY,
    allowedHosts: ALLOWED_HOSTS,
  },
});
