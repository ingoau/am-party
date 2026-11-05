import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend (FastAPI/etc.) runs on :8930 in dev. Override with ENCORE_BACKEND=http://host:port.
const backend = process.env.ENCORE_BACKEND ?? "http://localhost:8930";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/ws": { target: backend, ws: true, changeOrigin: true },
      "/api": { target: backend, changeOrigin: true },
      "/auth": { target: backend, changeOrigin: true },
      "/admin/apple-music/connect": { target: backend, changeOrigin: true },
      "/sendspin": { target: backend, ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
