import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Production builds set ``base: "/static/"`` so the emitted ``index.html``
// references assets at ``/static/assets/...`` — which is what Django +
// WhiteNoise serve in the single-origin deploy. Dev keeps ``base: "/"``
// so the Vite dev server on :5173 works unchanged.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_URL || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    base: mode === "production" ? "/static/" : "/",
    server: {
      port: 5173,
      proxy: {
        "/api": { target: backend, changeOrigin: true },
        "/healthz": { target: backend, changeOrigin: true },
      },
    },
  };
});
