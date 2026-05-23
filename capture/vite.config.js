import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Capture app (Phase B.1). Served plain http on localhost:8022; a cloudflared/
// ngrok tunnel terminates TLS and gives the phone a trusted https URL — the
// camera (getUserMedia, from Section 2 on) only works in a secure context.
// See ../PHASE_B1_CAPTURE_PROTOTYPE.md.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 8022,
    strictPort: true,            // fail loudly rather than drifting to another port
    // The browser only ever talks to this origin; Vite proxies /api to the
    // backend server-side, so uploads are same-origin and never hit CORS.
    proxy: {
      '/api': 'http://localhost:8020',
    },
    // The tunnel reaches us with a Host header like <random>.trycloudflare.com;
    // Vite's host check would otherwise reject it. Allow any host in dev.
    allowedHosts: true,
  },
});
