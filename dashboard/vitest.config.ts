import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

const templateRoot = path.resolve(import.meta.dirname);

const alias = {
  "@": path.resolve(templateRoot, "src"),
  "@contracts": path.resolve(templateRoot, "contracts"),
  "@assets": path.resolve(templateRoot, "attached_assets"),
};

// Two test surfaces under one `vitest run`:
//   • src/**  — the browser-React dashboard, runs in jsdom with RTL + jest-dom
//   • api/**  — the Hono/tRPC control-plane seam, runs in plain node
// Kept as separate projects so each gets the correct environment and the
// React plugin / jsdom setup never leaks into the node-side api tests.
export default defineConfig({
  resolve: { alias },
  test: {
    projects: [
      {
        plugins: [react()],
        resolve: { alias },
        test: {
          name: "dashboard",
          environment: "jsdom",
          globals: true,
          setupFiles: ["./src/test/setup.ts"],
          include: ["src/**/*.{test,spec}.{ts,tsx}"],
        },
      },
      {
        resolve: { alias },
        test: {
          name: "api",
          environment: "node",
          include: ["api/**/*.{test,spec}.ts"],
        },
      },
    ],
  },
});
