import path from "path"
const __dirname = import.meta.dirname
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"
import { inspectAttr } from 'kimi-plugin-inspect-react'

// https://vite.dev/config/
// AATS Sniper runs STANDALONE on mock data (src/lib/api.ts). The old Hono
// dev-server middleware (api/boot.ts → tRPC + MySQL) is intentionally NOT
// mounted: it would require the backend toolchain to boot `npm run dev`.
// Wire it back only alongside a running control plane (VITE_USE_MOCK=false).
export default defineConfig({
  plugins: [
    inspectAttr(), react()],
  server: {
    port: 3000,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@contracts": path.resolve(__dirname, "./contracts"),
      "@db": path.resolve(__dirname, "./db"),
      "db": path.resolve(__dirname, "./db"),
    },
  },
  envDir: path.resolve(__dirname),
  build: {
    outDir: path.resolve(__dirname, "dist/public"),
    emptyOutDir: true,
  },
});
