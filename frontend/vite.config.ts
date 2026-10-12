import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves on :5173 and proxies the API to the Python server on :8000
// (override with VITE_API=http://host:port, e.g. to point at a mock API).
// `npm run build` writes the production bundle to ../ui/dist, which FastAPI serves.
declare const process: { env: Record<string, string | undefined> };  // Node global (no @types/node needed)
const api = process.env.VITE_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../ui/dist", emptyOutDir: true },
  server: { proxy: { "/api": { target: api, changeOrigin: false } } },
});
