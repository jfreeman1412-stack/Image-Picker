import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,             // bind 0.0.0.0 so the dev server is reachable on the LAN
    port: 8021,
    strictPort: true,       // fail rather than silently moving to 8022 if 8021 is busy
    proxy: {
      // Same-origin proxy: remote browsers hit Vite, Vite forwards to the
      // backend on the host. Backend itself can stay on localhost.
      '/api': 'http://localhost:8020',
    },
  },
});
