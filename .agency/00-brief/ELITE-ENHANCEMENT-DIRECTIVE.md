# AATS — ELITE SNIPER / COMPETITOR-PARITY ENHANCEMENT DIRECTIVE

**Logged:** 2026-07-01 · **Source:** CEO directive (verbatim intent below) · **Owner:** Agency Runtime → orchestrator

## CEO intent (restated)
Take AATS from "paper-proven honest sniper" to a **best-in-class, elite** Solana meme-coin sniper —
studying how the top production bots (Trojan, BonkBot, Maestro, Banana Gun, Sol Trading Bot, GMGN, Photon,
Unibot, Pepe Boost) and community builders (ISAC, ryal6042, conceiv3d-in-lib3rty, Bilel_Smooth) actually
win — then enhance everything upgradeable. Add a model that reads meme-coin price paths, trained on the
shadow corpus. Then a step-by-step to link a wallet and trade live.

## NON-NEGOTIABLE GUARDRAILS (these OVERRIDE any competitor-inspired feature)
The following HARD RULES from the master charter are **not waived by this directive** and every enhancement
must comply or be rejected at review:
1. **No fabricated performance.** There is **NO win-rate metric** anywhere. We do **not** claim, target, or
   display a "success rate." The sole success metric stays: **net-of-cost PnL + model-vs-naive-baseline on
   RECORDED data.** Anyone who introduces a win-rate or a profit promise FAILS review.
2. **Edge-proof before capital.** Real capital stays `DRY_RUN`-disabled until the edge is PROVEN on the
   recorded corpus (R3 Block A) and the CEO authorizes live. No enhancement lifts this gate.
3. **Asymmetric trust.** Every new signal (KOL call, smart-money copy, chart-path model, sentiment) may only
   **DE-RISK** — reject, shrink, or exit. **Nothing may ever increase size, widen a stop, or add leverage.**
4. **LLM/heavy models never on the FAST/SNIPE hot path.** Chart-path / narrative models are SLOW-loop only.
5. **Point-in-time correctness (T-300a).** Event-time from on-chain data only; no wall-clock substitution.
6. **Money = integer lamports / Decimal, never float. No secrets in code/logs/images.**
7. **Custody:** dedicated, balance-capped hot wallet; isolated signer; key from Vault, never in env/logs.

## THE HONEST EDGE THESIS (distilled from the referenced material — the "logic")
The elite bots and the honest builders agree on a truth the marketing hides:
- **"Insider / 100% profit" is not real for public launches.** "Insider" = *speed of detection* +
  *front-running the predictable retail reaction*. It is NOT foreknowledge.
- **Two edges exist for a solo operator:** (1) **go fast** (first-block via Geyser/Jito — capital & infra
  heavy, brutally competitive) or (2) **go smart** (front-run the *predictable retail flood after a
  proven-KOL call / smart-money buy* — less crowded; ISAC's & ryal6042's approach). AATS should lean (2)
  with (1) as far as a solo setup honestly allows.
- **Predicting "which token moons" via ML fails** (Bilel_Smooth: 15k tokens, ~95% rugged, classifiers
  didn't work). What HAS signal: **de-risk classifiers**, **KOL/wallet track-record scoring**, and
  **reaction-timing exits** — not a profit oracle.
- **Costs are the silent killer** (ISAC: ~6% round-trip slippage; only ~10% of bots are consistently
  profitable). This VALIDATES the net-of-cost / edge-proof discipline already in the charter.
- **Exit discipline beats entry** (sell ladders, trailing TP, timed exits, dev-sell triggers; "all
  memecoins trend to zero; one big loss offsets many small gains").

## ENHANCEMENT CANDIDATES (to be audited, prioritized, then built dual-G3)
- **A. Alpha-engine (the "go-smart" edge):** Telethon KOL-call detection → CA extraction → point-in-time
  call events; smart-money wallet watcher via Geyser (copy-as-DE-RISK conviction, clamped ≤1); KOL/wallet
  track-record scoring feeding entry *selectivity* (never sizing up).
- **B. Rug/scam filter pack:** add dev-wallet-rugged-before, dev-wallet-funded-just-before-launch, bundle/
  sniper-cluster detection, holder/volume floors (augment existing blacklist/screener/safety-gate).
- **C. Exit-strategy engine:** take-profit ladders, trailing TP, partial sells, time-based exits, dev-sell
  auto-exit trigger — layered above the survivable stop (all DE-RISK).
- **D. Chart-path model (the CNN ask, done honestly):** SLOW-loop temporal/CNN regime classifier over the
  post-migration price path → accumulation / distribution / rug-in-progress → **de-risk & exit signal only**,
  calibrated probability + uncertainty, trained on the shadow corpus. NOT an entry profit-predictor.
- **E. Detection-latency hardening:** shred/first-block path + private-RPC/Geyser per the procurement report.

## FLOW
Audit-first (like E1–E13): fan-out gap analysis per domain vs the competitor matrix → prioritized roadmap →
build waves dispatched to specialized agents, each dual-G3 (code-reviewer + backtest-qa-engineer). The
edge-proof (R3 Block A) remains the gate before live. Wallet-linking/go-live runbook is honest and gated.
