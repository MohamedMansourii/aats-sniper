# AATS SNIPER — OPEN QUESTIONS FOR CEO (G0)

**Version:** 1.0.0
**Date:** 2026-06-16
**Author:** `quant-product-analyst`
**Purpose:** Every ambiguity that touches capital-at-risk, cost, scope, or legality.
Each question has a recommended default. BLOCKER = must be answered before G1 (architecture
begins). NON-BLOCKER = can be answered any time before the wave that uses it.

No silent assumptions. Where I have already made a defensible default, I state it;
where the CEO's answer changes capital-at-risk or scope, it is a BLOCKER.

---

## OQ-001 — Daily-risk-capital tranche definition
**Priority:** BLOCKER (gates FR-034, AC-028)
**Why it matters:** The circuit breaker threshold is "−3.0% of the day's allocated daily-
risk-capital tranche." If the tranche is undefined, the breaker threshold is undefined —
and a undefined breaker is no breaker at all.
**The ambiguity:** Does the CEO want the tranche to be: (A) a fixed SOL amount defined
once at wallet-funding time (e.g. total wallet balance = tranche), (B) a daily-reset
rolling allocation (e.g. each UTC midnight the tranche resets to max(wallet_balance, cap)),
or (C) a manually set daily budget the CEO enters each morning before trading begins?
**Options:**
1. (A) Fixed at wallet-funding time: simple, conservative. If the wallet drops due to
   losses, the absolute −SOL floor shrinks automatically (protective). Recommended for R3.
2. (B) Daily-reset to wallet_balance: straightforward automation; breaker tightens as
   wallet depletes. Standard in most professional systems.
3. (C) Manual daily budget: maximum control but adds operational friction; easy to forget.
**Recommended default:** Option (B) — daily-reset to wallet balance at UTC midnight, with
a hard-coded absolute floor of −0.30 SOL independent of the percentage calculation. The
CEO can override the percentage threshold at the `/api/risk-config` endpoint (tightening
only, never widening beyond the spec-stated 3.0%).
**Impact if not answered:** FR-034 threshold is under-specified; AC-028 cannot be tested.

---

## OQ-002 — Number and identity of smart-money wallets to track
**Priority:** NON-BLOCKER (needed before Lane A build, P3)
**Why it matters:** FR-007 requires an `accounts_subscribe` stream on a configured set of
profitable wallets. The size of this set directly impacts RPC subscription load (each
account is a separate sub) and influences the signal-to-noise of the `smart_wallets_in`
feature. EH-005 is classified expected-ZERO until measured — so this is a config
question, not a strategy question.
**Options:**
1. Start with a small, CEO-curated set of 10–50 known-profitable public wallet addresses
   that the CEO already monitors manually. Simple, no attribution risk.
2. Build automated profitable-wallet discovery (by on-chain PnL analysis) and maintain a
   dynamic top-N list. More complex, requires a separate discovery pipeline.
3. Treat smart-money tracking as an optional feature, disabled by default, enabled only
   after EH-005 shows positive lift on recorded data.
**Recommended default:** Option 3 — disabled by default; the CEO provides a starter list
of 0–20 public addresses at deploy time. The feature is labeled "EXPERIMENTAL — EH-005
expected-ZERO until measured." Lane A builds the infrastructure; the CEO populates the
list as evidence of lift accumulates.
**Impact if not answered:** Lane A scope estimate changes slightly; no capital impact.

---

## OQ-003 — Colocation and RPC provider decisions
**Priority:** BLOCKER before P2.5 (latency-devops build) but NON-BLOCKER for G1
**Why it matters:** The latency budget (NFR-001) and the competitive posture are
conditioned on the infra tier. A generic VPS gives 300+ ms ingress; a colo bare-metal
node with ShredStream gives 18–30 ms. The EDGE-VERDICT.md C-1 condition explicitly
requires the `latency-budget.md` document to state the solo floor in plain numbers —
those numbers depend on which tier is actually deployed. The architect needs a tier
commitment to design the deploy topology.
**Options:**
1. **Dedicated node + Yellowstone (tier: "dedicated_geyser"):** ingress ~60 ms, jitter
   ~25 ms. Moderate cost (~$200–500/month for a suitable provider). Adequate for
   safety-selective late-entry thesis (slot +5..+30 is not speed-sensitive).
2. **Colo bare-metal + ShredStream (tier: "colo_shred"):** ingress ~18 ms, jitter ~8 ms.
   Higher cost (~$500–1,500/month). Better for migration pre-staging; no win against
   staked N+0 co-bundlers regardless.
3. **Generic VPS + enhanced WS (tier: "generic_ws"):** ingress ~300 ms. Cheapest; adequate
   for shadow/record mode; barely viable for paper; NOT adequate for competitive snipe
   even on safety-selective late entry.
**Recommended default:** Option 1 (dedicated_geyser) for the initial deploy. The build
is tier-agnostic (pluggable InfraTier config); the operator can upgrade to colo_shred
later. For the paper/shadow rung, Option 1 is sufficient. The architect should design
the Docker Compose stack to run on either tier via config.
**Impact if not answered:** `latency-budget.md` (C-1) cannot be written with real numbers;
NFR-001 p50/p99 targets will need a caveat. Escalate before P2.5 dispatch.

---

## OQ-004 — Telegram bot token and operator Telegram user ID
**Priority:** NON-BLOCKER for spec; BLOCKER for Lane F build and deploy
**Why it matters:** AC-043 (Telegram authz) requires the authorized-operator Telegram user
ID to be configured. AC-042 requires the bot token. These are secrets — they go into
`.env.example` only. The architect needs to know the auth model.
**Options:**
1. Single operator Telegram user ID hard-configured in `.env` — simplest; appropriate for
   a solo operator.
2. A configurable list of authorized operator IDs — supports a small team but adds a tiny
   auth management surface.
3. Telegram group/channel with admin-role-based auth — most complex, not needed for a
   solo operator.
**Recommended default:** Option 1 (single user ID in `.env`). If the CEO wants a second
authorized user later, it is a one-line env change, not a code change.
**Impact if not answered:** Lane F cannot implement AC-043. No capital impact; authz gap
is a security risk if not answered before first Telegram deploy.

---

## OQ-005 — Maximum number of simultaneous open positions (per-coin cap and aggregate)
**Priority:** BLOCKER (gates FR-031 and FR-032 numeric thresholds)
**Why it matters:** FR-031 names "per-trade capital cap" and "max aggregate exposure" as
hardcoded floors that the risk engine enforces. FR-032 names "per_coin_cap". These are
capital-at-risk decisions. Without numbers, the risk engine has no hard floor to enforce.
**Options (illustrative; CEO sets the actual numbers):**
1. **Conservative R3 defaults:** per-trade cap = 0.1 SOL; max aggregate exposure = 0.5 SOL
   (5 simultaneous positions at max size); daily-risk tranche = 0.5 SOL.
2. **Moderate R3 defaults:** per-trade cap = 0.25 SOL; max aggregate = 1.0 SOL;
   daily-risk tranche = 1.0 SOL.
3. **Custom:** CEO specifies exact numbers at R3 authorization.
**Recommended default:** Encode Option 1 as the hardcoded floor in the spec and code.
Any values entered via `/api/risk-config` may only be ≤ these floors (tighten, never
widen). The CEO may widen them only by a config change at R3 authorization time —
that is an explicit CEO decision, not a runtime API call.
**Impact if not answered:** FR-031/FR-032 are under-specified; AC-030 cannot set a
meaningful threshold. Capital-at-risk is directly affected.

---

## OQ-006 — Dead-man's switch T_DMS (heartbeat absence threshold)
**Priority:** BLOCKER (gates AC-045, FR-033 Layer 3)
**Why it matters:** A T_DMS that is too short causes false-positive flattens (network
blip triggers) — bad. Too long leaves open positions unmanaged during a genuine crash —
potentially worse. 60 seconds is the recommended default in the spec; the CEO may want
a different value.
**Options:**
1. T_DMS = 30 seconds — aggressive; a network blip of > 30 s triggers a flatten. Safer
   for capital, noisier operationally.
2. T_DMS = 60 seconds — current recommended default. Balances false-trigger risk vs
   unmanaged-position risk.
3. T_DMS = 120 seconds — gentler; network blips less likely to trigger; longer exposure
   window in a crash.
**Recommended default:** T_DMS = 60 seconds. Configurable in `.env`; the architect
encodes it as an environment variable, not a hardcoded constant, so the CEO can tighten
it without a code change.
**Impact if not answered:** AC-045 cannot be tested with the correct threshold. Affects
risk (length of unmanaged-position window).

---

## OQ-007 — Adverse-selection haircut calibration: what to use BEFORE R1 fills exist
**Priority:** NON-BLOCKER for spec; BLOCKER for `backtest-qa-engineer` at R2
**Why it matters:** EDGE-VERDICT.md C-11 requires the haircut to be calibrated from
recorded R1 fills before GATE-A is computed at R2. But the spec must define what value
is used in the SHADOW mode cost-gate and in any early sim runs before calibration.
**Options:**
1. Use the CONSERVATIVE TOP of the band: 150 bps. This is C-2 compliant (floor,
   widened not narrowed). If edge appears at 150 bps, it is robust.
2. Use 75 bps (low end of band). Risk: overestimates edge before calibration.
3. Use 100 bps (midpoint). A compromise.
**Recommended default:** Option 1 (150 bps conservative top) as the pre-calibration
default, with a prominent UNCALIBRATED label in all pre-R1 reports. After R1 calibration
the measured value replaces it, and if measured > 200 bps the C-11 sub-gate fires.
**Impact if not answered:** Pre-R1 cost-gate is ambiguously specified. No immediate
capital impact (no capital at this stage), but it affects early shadow-mode decisions.

---

## OQ-008 — Exit mode default: Fast-MEV vs Secure-MEV
**Priority:** NON-BLOCKER (Lane C build)
**Why it matters:** FR-029 specifies both modes exist; FR-030 exposes presets. But the
DEFAULT mode for new entries (before the operator configures a preset) must be specified.
Secure-MEV has lower sandwich risk; Fast-MEV has lower base exit slippage (at higher
sandwich risk). For a solo operator prioritizing capital preservation, Secure is safer.
**Options:**
1. Secure-MEV as default. Consistent with capital-preservation priority.
2. Fast-MEV as default. Potentially better base price at higher sandwich risk.
3. Per-position adaptive (SLOW-loop selects based on pool depth). Most complex.
**Recommended default:** Option 1 (Secure-MEV as default). The operator can change to
Fast-MEV via Settings preset. EH-002's exit-haircut validation will measure the realized
delta.
**Impact if not answered:** FR-029/FR-030 default config is ambiguous. No capital impact
until live trading.

---

## OQ-009 — Jurisdiction and legal classification
**Priority:** NON-BLOCKER for build; BLOCKER before CEO authorizes R3 (real capital)
**Why it matters:** Autonomous trading bots that trade on behalf of a person may be
subject to financial regulations depending on the operator's jurisdiction (e.g. MiFID II
in EU, SEC/CFTC in US for securities/commodities, MSB registration for crypto trading
bots in some states). The spec makes no legal representation; the CEO must confirm their
jurisdiction's compliance posture before real capital is deployed.
**Options:**
1. CEO confirms they have taken independent legal advice for their jurisdiction and that
   operating the bot for personal trading is legal and compliant. Build proceeds.
2. CEO has not yet confirmed legal advice; we add a warning in the docs that real-capital
   operation requires legal review. Build proceeds; R3 authorization is withheld until
   confirmed.
3. Legal review concludes operation is restricted; CEO halts the project or restructures.
**Recommended default:** Option 2 — `docs-delivery` adds a legal-disclaimer section to
the README and deploy guide stating that the operator is responsible for confirming
compliance with local law before enabling real-capital trading. The spec does not block on
this; R3 CEO authorization implicitly includes this confirmation.
**Impact if not answered:** No code impact. Capital-at-risk if the CEO operates in a
restricted jurisdiction without realizing it. This is a CEO-decision, not a spec-decision.

---

## OQ-010 — Multi-wallet configuration: maximum N wallets and per-mint blast-radius cap C
**Priority:** NON-BLOCKER for spec; BLOCKER before FR-036 / AC-024 are testable
**Why it matters:** FR-036 says "up to N independent trade-only signing wallets where N is
an operator-configured parameter ≤ CEO-authorized max." AC-024 says "total SOL allocated
across all wallets to mint X MUST NOT exceed C SOL." Without the CEO specifying the range
of N and a starting C, these requirements are untestable.
**Options:**
1. N_max = 1 (single wallet) for R3 tiny-real; expand to multi-wallet only after edge
   is proven at single-wallet size. C = per_trade_cap (same as single-wallet default).
2. N_max = 3, C = 3 × per_trade_cap. Allows modest distribution at minimal operational
   complexity.
3. N_max = 10, C configurable. Full multi-wallet from the start.
**Recommended default:** Option 1 for R3 tiny-real — one wallet, no complexity. Multi-
wallet (Option 2 or 3) is enabled as an optional feature tested in the integration harness
but not activated at the first real-capital rung. The CEO activates it at R4.
**Impact if not answered:** AC-024 numeric threshold (C) is undefined; test harness cannot
be run.

---

## SUMMARY TABLE

| # | Question | Priority | Recommended Default | Capital Impact |
|---|---|---|---|---|
| OQ-001 | Daily-risk-capital tranche definition | BLOCKER | Daily-reset to wallet balance at UTC midnight + −0.30 SOL hard floor | HIGH |
| OQ-002 | Smart-money wallet list size and source | NON-BLOCKER | 0–20 CEO-provided; EH-005 disabled by default | LOW |
| OQ-003 | Colocation and RPC provider tier | BLOCKER (before P2.5) | dedicated_geyser tier (~$200–500/month) | MED |
| OQ-004 | Telegram bot token and operator user ID | NON-BLOCKER (for spec) | Single user ID in `.env`; Option 1 | LOW |
| OQ-005 | Per-trade cap and max aggregate exposure | BLOCKER | per-trade = 0.1 SOL, aggregate = 0.5 SOL, tranche = 0.5 SOL | HIGH |
| OQ-006 | Dead-man's switch T_DMS seconds | BLOCKER | 60 seconds, env-configurable | MED |
| OQ-007 | Pre-calibration adverse-selection haircut | NON-BLOCKER | 150 bps (conservative top), labeled UNCALIBRATED | LOW |
| OQ-008 | Default exit mode (Fast vs Secure MEV) | NON-BLOCKER | Secure-MEV default | LOW |
| OQ-009 | Jurisdiction / legal classification | NON-BLOCKER (build); BLOCKER (R3) | CEO legal review required before R3; doc disclaimer added | CEO-DECISION |
| OQ-010 | Multi-wallet N_max and blast-radius cap C | NON-BLOCKER (spec); BLOCKER (FR-036 test) | N_max = 1 for R3; Option 2 at R4 | MED |

**Blockers before G1:** OQ-001, OQ-003, OQ-005, OQ-006 must be answered before the
architect can complete the blueprint with real numeric thresholds.

**Blockers before real capital (R3):** All of the above PLUS OQ-009 (legal).
