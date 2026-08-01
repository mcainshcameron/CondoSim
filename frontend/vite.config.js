import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    // Dev-only: proxy /api to the FastAPI backend so the browser stays
    // same-origin. Production serves the built SPA from FastAPI itself, so
    // there is no proxy there and `api.js` resolves BASE to ''.
    //
    // A proxy rather than VITE_API_BASE=http://127.0.0.1:8001 because the
    // backend mounts no CORS middleware: cross-origin XHR would be blocked,
    // and `credentials: 'same-origin'` would drop the session cookie once
    // ADMIN_PASSWORD/SESSION_SECRET are set.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        // The SSE bus (/api/runs/{id}/events) is a long-lived stream;
        // buffering it would stall every event until the day ended.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type'] || '')
                .includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform';
            }
          });
        },
      },
    },
  },
});
