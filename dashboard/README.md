# AATS Sniper — operator dashboard

A premium, dark, Solana meme-coin ultra-sniper **operator command deck** — a pro
trading-terminal UI (think Photon / Axiom / BullX / GMGN), not a generic admin
template. Near-black canvas, hairline borders, one vivid emerald accent, tabular
monospace numerics, and color that always encodes meaning (green up / red down /
amber warn / blue info / violet model).

Built on React 18 + Vite + TypeScript + Tailwind v3 + shadcn/ui. Recharts for
charts, lucide-react for icons, react-router for navigation, sonner for toasts.

## Runs standalone on mock data

The dashboard renders entirely from a realistic mock telemetry layer
(`src/lib/mock.ts`) — **no backend, tRPC server, or MySQL is required** for
`npm run dev`. An evolving snipe feed, live-ish metrics, positions, latency
budget, sentiment, predictions, reasoning, risk config, and module health all
stream from the mock client.

When a control plane is available, flip `VITE_USE_MOCK=false` and the typed
client (`src/lib/api.ts`) fetches/streams the operator API instead
(`/api/state`, SSE `/api/feed`, `/api/metrics`, `/api/positions`, `/api/latency`,
`/api/sentiment`, `/api/predictions`, `/api/reasoning`, `/api/risk-config`,
`/api/health`, and the `POST` actions `/api/kill`, `/api/flatten`,
`/api/breaker/reset`, `/api/mode`). Set the base URL with
`VITE_CONTROL_PLANE_URL`. See `.env.example`.

## Run

```bash
npm install      # install dependencies
npm run dev      # start the dev server at http://localhost:3000 (mock data)
```

Other scripts:

```bash
npm run build    # type-check (tsc -b) then production build to dist/public
npm run preview  # preview the production build
npm run lint     # eslint
```

## Pages / routes

| Route          | Page         | Purpose                                              |
| -------------- | ------------ | ---------------------------------------------------- |
| `/`            | Command deck | KPIs, live loops, connection + wallet at a glance    |
| `/feed`        | Snipe feed   | Live stream of snipe events, gate verdicts, actions  |
| `/latency`     | Latency      | Per-hop latency budget vs slot floor; infra tiers    |
| `/positions`   | Positions    | Open + closed positions, TP ladder, exit modes       |
| `/sentiment`   | Sentiment    | Multi-component sentiment (MCS) scores per asset      |
| `/model`       | Model        | Classifier probability, calibration, feature weights |
| `/reasoning`   | Reasoning    | LLM signal log, vetoes, narrative-failure flags      |
| `/risk`        | Risk         | Editable risk config, daily-loss gauge, breaker      |
| `/monitoring`  | Monitoring   | Module health, latencies, staleness                  |
| `/settings`    | Settings     | Operator settings                                    |

Routes are code-split (lazy-loaded) so each page and its charts load on demand.
