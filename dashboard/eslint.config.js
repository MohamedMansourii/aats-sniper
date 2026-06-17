import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  // `dist` is build output. `api/**` is the Hono/tRPC backend control plane —
  // a separate seam with its own owner and Node runtime; it is not part of the
  // browser-React dashboard surface this config governs, so it is not linted
  // here (it would otherwise be checked against browser globals).
  globalIgnores(['dist', 'api/**']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
  },
  {
    // Vendored shadcn/ui primitives (generated, not authored here). They
    // intentionally co-export variant helpers (e.g. `buttonVariants`) beside
    // their component — a Vite fast-refresh DX nit that does not apply to a
    // production build — and a couple use Math.random() for skeleton widths.
    // We keep these as upstream ships them rather than fork every primitive.
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
      'react-hooks/purity': 'off',
    },
  },
])
