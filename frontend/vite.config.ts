import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The console is served by FastAPI (`di/app.py::_mount_frontend`), which mounts
 * `/assets` from `frontend/dist/assets` and falls back to `dist/index.html` for
 * every non-`/api` path. `base: './'` keeps asset URLs relative, and Vite's
 * default `assets/` output dir lines up with the StaticFiles mount.
 *
 * ROUTE-DEPTH CONSTRAINT — read before adding a route.
 * `base: './'` emits `<script src="./assets/index-*.js">`. Because the SPA
 * fallback serves that same index.html at any path, the browser resolves that
 * relative URL against the *current* URL:
 *   /documents        -> /assets/index-*.js        OK
 *   /jobs/abc-123     -> /jobs/assets/index-*.js   404 -> fallback returns
 *                        index.html as text/html -> module fails -> blank page.
 * So every route must be exactly ONE segment deep. Job detail is therefore
 * `/job?id=...` rather than `/jobs/:jobId`. If you ever need a deeper route,
 * switch to `base: '/'` (safe here: app.py hardcodes the /assets mount at the
 * server root, so the bundle is not actually relocatable anyway) or adopt a
 * HashRouter.
 */
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
  },
  server: {
    port: 5173,
    proxy: {
      // `npm run dev` talks to a locally running backend.
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/readyz': { target: 'http://localhost:8080', changeOrigin: true },
      '/health': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
});
