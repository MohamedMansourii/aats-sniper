# EDGE-VERDICT — AATS Solana Meme-Coin Ultra-Sniper

**Author:** `quant-research-lead` (Lead Quantitative Architect)
**Stage:** P0 — pre-G0 edge gate
**Date:** 2026-06-16
**Supersedes:** the prior winnable-edge verdict carried in `AATS-BRIEF.md` §7.3 — this re-confirms it with current (2026-06) ground truth and tightens the gates. It does not overturn it.

---

## VERDICT (first three lines, unambiguous)

1. **GO-PAPER-ONLY.** There is a *plausible, structurally-defensible* edge on a narrow set of surfaces (safety-selective late entry + exit discipline + migration-survivor selection + smart-money-as-filter), but it is **UNPROVEN on recorded data**. Real capital stays disabled.
2. **The edge is NOT speed.** A solo operator **cannot win the block-0 / migration-block-0 latency war** against co-located, staked, insider co-bundling bots — confirmed current: bots now "trigger migration and place the first buy on the new pool in one atomic tx." Any thesis that requires being first to a pool is **NO-GO** and is killed here.
3. **The named adversary if we are wrong:** the **N+0 insider/co-bundler** (LP-add or migration crank co-signer) and the **co-located staked MEV bot** — by the time our buy is on-chain we are their **exit liquidity**. We win only on surfaces where *they have already chosen not to hold*: avoiding the rugs they dump, and harvesting the survivors they front-ran into.

> Decision rule for the CEO: proceed to G0/spec and build the **paper/shadow-record** system. **Do not authorize one lamport of real capital** until both acceptance metrics (§4) clear on RECORDED data through the capital-staging gates (§6). If the recorded-data gate fails, the honest deliverable is "no edge net of cost" — and that is a successful project outcome, not a failure.

---

## 1. Why GO-PAPER-ONLY and not GO, not NO-GO

**Not NO-GO**, because the surfaces below are not speed races — they are *selection and discipline* problems where a slower, more careful operator can have positive expectancy. The sim already demonstrates the *mechanism* (exit discipline beats naive exits; a gate avoids catchable rugs; tips bounded by edge stop the bleed). The mechanism is real; the magnitude is unproven.

**Not GO**, because every favorable number we have is from a sim with two acknowledged fictions (`AATS-BRIEF.md` §7.2): a **synthetic launch distribution** and an **emulated model skill** (`model_prob` reads truth through a noise channel — it is not a predictor). You cannot grant real capital on a model that is, by construction, cheating. The brief's own §7.5 forbids it: scale only when **net-of-cost PnL AND model-vs-naive-baseline are BOTH positive on RECORDED data.** We are pre-recording. Therefore the only honest verdict is GO-PAPER-ONLY.

**Established facts re-confirmed this session (cited, dated):**
- pump.fun graduations migrate to **PumpSwap** automatically and irreversibly at ~$69k mcap (~85 SOL of buy volume), **no migration fee**, 0.25% AMM fee (0.20% LP / 0.05% protocol). Migration is a **deterministic, pre-stageable trigger** — good for us. (cryptoadventure, blocmates, pump.fun/docs/fees, 2026.)
- **ShredStream gives 50–200ms pre-confirmation detection** and is **standard on dedicated MEV nodes in 2026** — i.e. it is *table stakes, not an edge*. Having it lets us play; it does not make us faster than the pros who also have it. (Chainstack, Jito docs, 2026.)
- Migration sniping is **industrialized**: bots co-bundle the migration crank with the first PumpSwap buy atomically. **Migration-block-0 is a losing race for a solo desk.** (MoonHydra, JUMPBIT, Bitquery, 2026.)
- Jito tip percentiles **rot weekly**; the only correct design is to read the live `tip_stream` and bound the tip by edge — never hardcode. (Jito docs.)

---

## 2. Edge hypotheses — one per defensible surface

Each `EH-NNN` states: the claimed inefficiency, the entry/exit it implies, the **expected edge in bps NET of all costs**, the data needed to test it, and the **pre-registered kill condition** (the disconfirming result that ends the hypothesis). No hypothesis here without a kill condition.

> Net-bps numbers are **hypotheses to be measured, not promises.** They are the bar the surface must clear on recorded data, not a claim that it will. The cost stack they are netted against is in §3.

---

### EH-001 — Safety-selective late entry (the primary thesis)
- **Inefficiency:** ~60% of launches are traps; a meaningful fraction of rugs carry **on-chain-detectable signatures** at event-time (freeze authority, un-renounced mint, unburned/short-locked LP, dev/bundle cluster, sell-tax). Fast bots optimize for *speed*, not for *not-getting-rugged*; they eat detectable rugs they could have skipped. By entering at **slot +5..+30** (deliberately late) we trade away speed we cannot win for **selection we can**.
- **Entry:** only on launches passing the 0-RPC hot gate (checks 1–5) AND the model probability threshold; size capped per coin; never race the tip.
- **Exit:** ExitEngine (TP ladder + trailing + hard stop + timeout), Secure-MEV mode.
- **Expected edge (hypothesis, NET):** **+150 to +400 bps per traded launch** sourced almost entirely from **rug avoidance** (turning a cohort of −10000 bps outcomes into skipped/−0 outcomes), NOT from out-trading anyone. Below +120 net it does not clear the cost stack at target size.
- **Data needed:** recorded first-K-slot snapshots for several thousand launches with **point-in-time** safety features; honest post-hoc rug labels (LP-pull / sell-disable within horizon) computed in **event-time**.
- **KILL CONDITION (pre-registered):** if, on recorded data, the gate's **rug-avoidance does not produce a net-PnL improvement over the no-gate baseline** (i.e. selected cohort net PnL ≤ unselected cohort net PnL after costs), OR catchable-rug recall < 0.50 at the chosen operating point, this surface is **dead** — late entry without a working gate is just being slow.

### EH-002 — Exit discipline (the most-proven lever in the sim)
- **Inefficiency:** most snipers die on the *exit*, not the entry — they round-trip winners back to zero or panic-dump floors. A disciplined staged exit (partial TP ladder + armed trailing + hard stop) harvests the asymmetric upside that does occur.
- **Entry:** inherited from whichever entry surface fired.
- **Exit:** `SECURE_EXIT` ladder; private routing to cut sandwich probability.
- **Expected edge (hypothesis, NET):** the sim shows TP-ladder beats naive 0.5×-peak by ~+24% on identical fills; treat the *delta* as real-direction, magnitude unproven. **Bar: staged exit must beat naive exit by ≥ +10% net PnL on recorded paths**, after exit slippage + sandwich haircut.
- **Data needed:** recorded post-entry price paths (tick/slot-level) for traded cohort; realized sandwich incidence on sells by MEV mode.
- **KILL CONDITION:** if staged exit does **not** beat the naive baseline exit by ≥ +10% net on recorded paths, OR the hard stop fails to fire inside its budget on rug paths in QA, revert to the simplest exit and **strip the "exit edge" claim entirely.**

### EH-003 — Migration-survivor selection (NOT migration-block-0)
- **Inefficiency:** graduation is a deterministic, pre-stageable event, BUT block-0 of the new PumpSwap pool is owned by atomic co-bundlers (confirmed 2026). The exploitable inefficiency is **post-migration survivorship**: graduated tokens that hold liquidity and clear an early-survival filter (no immediate LP pull, holder dispersion improving, dev not dumping) in the **first minutes** after migration, entered *deliberately after* the block-0 melee with the disorderly early dumpers already flushed.
- **Entry:** trigger on migration event (pre-staged pool keys), but **enter at +5..+30 slots / first-minutes window**, conditioned on survival features, never block-0.
- **Exit:** ExitEngine.
- **Expected edge (hypothesis, NET):** **+100 to +300 bps per qualifying migration**, sourced from avoiding the post-migration dump cohort and riding genuine continuation. Below +100 net it is noise.
- **Data needed:** recorded migration events + first-minutes microstructure; survival labels at a fixed event-time horizon.
- **KILL CONDITION:** if qualifying-migration cohort net PnL ≤ 0 on recorded data, OR if the only profitable migration entries are at slot delay ≤ 1 (i.e. you must win the race to profit — which we cannot), this surface is **dead.**

### EH-004 — Coin-profile specialization
- **Inefficiency:** a single global model under-fits a heterogeneous launch population. Conditioning entry policy on coin profile (initial LP depth band, holder-distribution shape, creator history, venue) should raise selection precision.
- **Entry:** per-profile thresholds layered on EH-001/EH-003.
- **Exit:** ExitEngine, profile-tuned ladder allowed (de-risk direction only).
- **Expected edge (hypothesis, NET):** an **incremental +30 to +100 bps** over the un-segmented policy — a *refinement*, not a standalone surface.
- **Data needed:** recorded launches with profile features + outcomes, enough per profile bucket to avoid overfitting (min N per bucket set in walk-forward spec).
- **KILL CONDITION:** if segmentation does **not** beat the pooled model out-of-sample on purged CV (per-bucket net PnL ≤ pooled), it is **overfitting** — discard the segmentation and keep the pooled policy.

### EH-005 — Smart-money as a SELECTIVITY filter / trigger ONLY (never a blind mirror)
- **Inefficiency:** a set of historically-profitable wallets carries weak predictive information about *which* launches are worth attention. Used as a **filter** (raise conviction / shortlist) or a **risk-off trigger**, it adds selectivity. Used as a **mirror** it is a guaranteed loss — by the time their buy is on-chain we are behind their fill and are their exit liquidity.
- **Entry:** `smart_wallets_in ≥ N` may *gate in* a candidate or *raise* a threshold-pass; it may NEVER auto-buy, NEVER size up, NEVER widen a stop. It is an adversarial input (could be a wallet farming copy-traders).
- **Exit:** unaffected; smart-money *exit* may only de-risk (force/accelerate our exit), never delay it.
- **Expected edge (hypothesis, NET):** **+0 to +80 bps** as a filter. **The disconfirming default is +0** — assume no lift until measured.
- **Data needed:** recorded smart-wallet entry/exit timestamps vs our event-time; measured **lift on net PnL of the filtered cohort vs unfiltered**, with explicit lag accounting (we are always behind them).
- **KILL CONDITION:** if the filtered cohort's net PnL ≤ unfiltered after accounting for our entry lag, the signal is **dead** (or is a copy-trap). If it is ever wired as a buy trigger or risk-increaser, that is a **HARD-RULE violation and a release blocker**, not a tuning choice.

---

## 3. Cost model — the round-trip stack every edge is netted against

Entry rule (non-negotiable, encoded in the risk gate):
> **`expected_edge_bps > total_cost_bps` or NO TRADE.** Gross-edge claims are rejected on sight.

`total_cost_bps` per round trip =

| Component | Treatment | Note |
|---|---|---|
| **Jito tip** | Read LIVE from `bundles.jito.wtf/.../tip_stream` (25/50/75/95/99 pct). Bid bounded by `min(market_floor, edge_cap)` where `edge_cap = 0.30 × expected_edge_sol`. **Never hardcode.** | A tip is pure cost; it buys landing odds, not a better price. If `edge_cap < floor`, the race is unwinnable at a competitive tip → **do not enter.** (`tips.py` already enforces this.) |
| **Priority / CU fee** | `cu_price_microlamports × cu_limit / 1e6`. Set CU limit tight; price from recent landed-priority percentiles. | Small vs tip but non-zero; counted. |
| **Entry slippage** | Modeled against untouched spot via `buyers_ahead` co-buyers (constant-product). At target size on thin new pools this is the dominant variable cost. | This IS adverse selection at entry — you fill *after* faster buyers' impact. |
| **Round-trip AMM fee** | 0.25% PumpSwap / Raydium each side (2026, confirmed). | 50 bps round trip baseline before anything else. |
| **Exit slippage + sandwich haircut** | `exit_slippage` + `sandwich_p × sandwich_loss`. Secure mode lowers `sandwich_p` (~0.08) at a slightly worse base price; Fast mode is cheaper base but ~0.30 sandwich. | Modeled per `exits.py`. |
| **Adverse-selection haircut (explicit)** | **Subtract an additional haircut representing "you fill worst exactly when you are most right least."** Provisional **75–150 bps** at target size on first-K-slot entries, to be **calibrated from recorded fills** (realized slippage conditional on subsequent adverse move). Until calibrated, use the conservative top of the band. | This is the line item that kills naive backtests. It is mandatory, not optional. |

**Illustrative floor:** AMM round trip (50 bps) + entry slippage (var) + tip+priority (var, edge-bounded) + adverse-selection haircut (75–150 bps) ⇒ a candidate must clear roughly **150–300+ bps net** before any surface's hypothesized edge is real. Every EH-NNN net number above is stated against this stack.

---

## 4. Success metrics — the machine-checkable acceptance gate

The gate is **binary and dual**. BOTH must hold on **RECORDED** data (never synthetic) at the staging window in question:

**GATE-A — Net-of-cost PnL > 0.** Σ(realized PnL) − Σ(tips + priority + slippage + AMM fees + adverse-selection haircut) > 0, with a **lower 95% bootstrap confidence bound > 0** over the walk-forward test windows (a point estimate is not enough — see methodology).

**GATE-B — Snipe model beats the naive-momentum baseline.** The snipe classifier's selected-cohort **net PnL per unit risk** must exceed the **naive-momentum baseline** (defined in `walk-forward-methodology.md`) by a margin whose lower 95% bound > 0, on the same purged/embargoed test windows. *If the model cannot beat dumb momentum net of cost, there is no model* — this is one of the two controls that matter most.

Supporting instrumentation (engineers + QA wire these; they are diagnostics, NOT acceptance gates on their own — none may be tuned-toward as a win-rate target):
- **Land rate** and **time-to-land (ms / slot-delay)** — sanity on execution, not a goal.
- **Slot-delay-vs-winner** — confirms we are NOT pretending to win block-0.
- **Rug-avoidance rate** (catchable-rug recall at operating point) — the engine of EH-001.
- **Calibration (reliability curve)** of the snipe classifier — demand calibration evidence, not accuracy; a miscalibrated probability is a broken gate.
- **Model-vs-baseline net-PnL delta** — the headline number, surfaced on Grafana and the operator dashboard.

> **HONESTY CLAUSE (binding):** no fixed win-rate is ever a target, claim, or tuning objective. A probability threshold is a *gate*, not a promise of realized wins. Any metric, model, or report that rewards a manufactured win rate — or rewards manufactured hype/synchronous shilling — is a **bug and a release blocker.**

---

## 5. Kill criteria — when to halt or de-scope

**Two controls dominate; weakening either is a release blocker:**

**(K-0) Daily-loss circuit breaker — HARD HALT.**
- **Trip at −X% of allocated daily risk capital** (concrete starting number: **−3.0% of the day's allocated tranche, or −0.30 SOL on the tiny-real wallet, whichever is hit first** — tightened, never loosened, by the CEO at funding time). On trip: **stop all new entries, flatten or hand open positions to survivable-stop enforcement, and require manual re-arm.** The breaker is owned by `risk-guardrails-engineer`; the dead-man's switch (heartbeat loss → flatten/stop) backs it so the halt holds **even if the bot process dies.** The LLM may *trip* the breaker early (de-risk) but may **never reset or raise it.**

**(K-1) Model-vs-baseline collapse.** If GATE-B fails on the trailing test window (model ≤ baseline net of cost), **de-scope to baseline / halt model-driven entries** until re-proven. The model loses its license to trade the moment it stops beating dumb momentum.

**Per-surface decay triggers (auto-halt the surface, alert operator):**
- **Consecutive losses:** N consecutive net-losing trades on a surface (start **N = 8**) → pause that surface, require review.
- **Land-rate collapse:** land rate falls below floor (start **< 35%**) sustained → infra/contention problem; stop racing.
- **Rug-avoidance decay (EH-001/003):** catchable-rug recall drops below **0.50** on recent labeled outcomes → gate degraded, halt selective-entry until retrained.
- **Slippage / adverse-selection blowout:** realized adverse-selection haircut exceeds the calibrated band by >50% sustained → you have become exit liquidity; halt.
- **Regime break:** distribution shift in launch population (e.g. graduation threshold, venue, or tip regime changes) detected by drift monitor → freeze, re-validate on fresh recorded data before resuming.
- **Smart-money filter inversion (EH-005):** filtered-cohort net PnL turns ≤ unfiltered → disable the filter (likely a copy-trap forming).

**Who/what halts:** `risk-guardrails-engineer`'s circuit breaker + dead-man's switch execute the halt autonomously; the operator (dashboard / Telegram) can trip but only **de-risk** (kill / flatten / pause). No automated path and no LLM can re-arm, size up, or widen a stop.

---

## 6. Capital-staging plan — proven-edge gate between every rung

Fractional-Kelly sizing throughout: **size = min(per-coin cap, ¼ × Kelly_fraction)**, **hard cap ≤ 1/4 Kelly, never full Kelly.** The LLM and any signal may **shrink** size; **none may ever grow it.** Real capital is **DISABLED by default** behind a DRY-RUN flag and is enabled only by explicit CEO authorization *after* the recorded-data gate passes.

| Rung | What runs | Capital | GATE TO PASS to advance (all on the data type stated) |
|---|---|---|---|
| **R0 — Sim** | Current `sniper_sim` harness; mechanism studies. | None (synthetic). | Mechanism demonstrated: gate avoids catchable rugs, staged exit > naive, tips edge-bounded. **Synthetic — proves direction only, NEVER licenses capital.** |
| **R1 — Shadow / record** | Live ingestion in SHADOW mode: record point-in-time first-K-slot snapshots + would-be decisions; **submit nothing.** | None (real data, no orders). | **≥ ~3,000 recorded launches** with point-in-time features + event-time outcome labels; leak audit clean (`backtest-qa-engineer`); baseline + model both computable on this set. |
| **R2 — Paper / dry-run** | Full triple loop vs SimulationVenue **driven by recorded launches**; JitoJupiterVenue in DRY-RUN/no-submit (quote→build→sign→DON'T-send). | None (paper). | **GATE-A AND GATE-B both pass** on the **purged/embargoed walk-forward test windows** (≥ the window count in the methodology), lower 95% bound > 0 on both. Safety: circuit breaker, survivable stop, dead-man's switch all fire on demand in QA. |
| **R3 — Tiny-real** | Live submit, **capped throwaway wallet the CEO can fully lose** (suggest **≤ 2 SOL** total, ≤ 0.1–0.25 SOL/coin, ¼-Kelly). | Real, incinerable. **CEO explicit authorization required.** | After a fixed live sample (**≥ 100 real trades** across ≥ 2 walk-forward windows): live GATE-A AND GATE-B both hold, lower 95% bound > 0; realized adverse-selection haircut within calibrated band; no breaker-trip pathology. |
| **R4 — Scale** | Stepwise size increase. | Larger, still bounded. | Each step requires a **fresh passing walk-forward window at the new size** (slippage/adverse-selection scale with size — re-prove, never extrapolate). Any failed gate at a step → **revert to prior size**, never push through. |

**No path reaches R3 without a passing walk-forward result on recorded data.** This is the brief's §7.5 and §5 honesty clause encoded as a ladder. Scaling capital while edge is unproven is the one decision I do not make alone — it returns to the CEO as `NEEDS-CEO-DECISION`.

---

## 7. Where a solo operator simply cannot win (stated plainly)

- **Block-0 of any new pool** — owned by N+0 insiders co-bundling with the LP-add. Do not race it.
- **Migration-block-0 of PumpSwap** — owned by atomic migration-crank co-bundlers (confirmed 2026). Do not race it.
- **Pure tip-escalation auctions** — escalating tips into a latency war just subsidizes validators; `tips.py` caps the tip at 0.30× edge for exactly this reason.
- **Out-running ShredStream-equipped pros** — ShredStream is table stakes in 2026; it lets us *play*, it does not make us *faster than them*.

The realistic niche is the **inverse of the speed race**: be the patient desk that skips the detectable rugs the fast bots eat, enters the survivors after the block-0 melee clears, and exits with discipline. That is selection and risk, not latency — and it is the only place the numbers can come out positive net of cost.

---

## 8. Handoff requirements for downstream agents

- `quant-product-analyst` (G0): turn GATE-A/GATE-B and the §6 ladder into numbered, measurable acceptance criteria; carry the HONESTY CLAUSE verbatim; no win-rate criterion anywhere.
- `solana-systems-architect` (G1): the cost stack (§3) and the live-tip-stream requirement are architecture constraints; real venue stays behind the DRY-RUN flag; point-in-time feature store is mandatory.
- `ml-prediction-engineer`: model must output **calibrated probability + uncertainty** (reliability curve required), run in the latency budget, and be validated against the naive-momentum baseline of `walk-forward-methodology.md`. The LLM may only de-risk.
- `backtest-qa-engineer`: enforce `walk-forward-methodology.md` cold; GATE-A/GATE-B with lower-95%-bound > 0 is the pass bar at R2 and R3.

---

*Validation methodology that QA enforces: `./walk-forward-methodology.md` (same folder).*

---

## Red-team review and final verdict

Three independent red-teams reviewed this verdict against the actual sim source (`sol-sniper/sniper_sim/`) and live 2026 ground truth. **All three returned `GO-PAPER-ONLY`; none recommended halt.** I verified every code-level claim against the source files this session — the citations are accurate (`venue.py` `_competitor_delay`, `exits.py` SECURE constants, `safety.py:43` truth-read, `tips.py:33` floor fallback, `types.py` `LaunchEvent` has no price/volume series). I re-fetched ground truth: **SWQoS reserves 80% of leader QUIC connections for staked nodes (~83% first-block hit rate); an unstaked solo desk lives in the contested 20% lane** (Helius, Chorus One, Everstake, 2026). This directly confirms the central MEV critique.

**The red team did not move the verdict — it confirmed it (still GO-PAPER-ONLY) and exposed that several favorable sim numbers are not merely magnitude-uncertain but DIRECTION-contaminated by shared optimistic constants.** That makes the paper-record gate *more* necessary, not less. I accept the large majority of flaws; I rebut only where the critique over-reaches.

### Critique 1 — MEV & latency red-team (can a solo, unstaked desk realize the claimed edge?)

**1A. `colo_shred` sim places the solo desk N+1 TIED with the 60% pro pack; the network gives that lane to the 80% staked cohort it does not have. [HIGH] — ACCEPTED (verified).**
Confirmed in source: `my_delay = ceil((18+8+3)/400) = 1`, and `_competitor_delay()` puts only 15% of pros at N+0, 60% at N+1. The sim therefore counts `buyers_ahead` as ~15% insiders + same-slot tip-losers, omitting the 60% of pros who in reality submit on the staked QUIC/SWQoS lane and land a full slot earlier. Live ground truth (80% of QUIC reserved for staked, ~83% staked first-block hit) makes this a structural understatement, not a tuning quibble. **Action: the honest solo floor is reframed as `DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED`. The recorded-data `buyers_ahead` distribution must be shifted right by ~one slot of pro/staked traffic, and the §3 adverse-selection haircut (75–150 bps) is re-designated a FLOOR to be widened, never a band to be narrowed, until live R3 fills measure it. The sim's `_competitor_delay()` MUST NOT be inherited by the R1/R2 recorded cost stack** (see condition C-2).

**1B. EH-002 exit cost models the wrong failure mode (classic sandwich, not insider/LP-puller dump-into-sell on thin pools). [HIGH] — ACCEPTED (verified).**
Confirmed: `SECURE_EXIT` hardcodes `sandwich_p=0.08, sandwich_loss=0.04` and `_sell()` applies only that. A private bundle removes public front-running but does nothing against an insider holding size who dumps in the same/adjacent slot, nor against own constant-product impact on a thin curve. The +24% sim delta is computed on synthetic `generate_path` with a fixed per-sell haircut the real thin-pool exit will exceed. **Action: EH-002 demoted (see "EH framing corrections" below); its exit haircut must be re-modeled with insider/LP-puller dump-into-sell on recorded post-entry paths before the +10%-net bar is honored.**

**1C. Edge-bounded tipping induces a cohort-selection spiral: the desk is priced OUT of the contested high-EV launches and INTO the launches the pros declined. [HIGH] — ACCEPTED (new, structural).**
Confirmed in `tips.py:33`: when `edge_cap <= floor` the strategy returns `floor` and "the metrics will show it bleeding" — i.e. it declines to compete. On the highest-EV migration-survivor / safety-selective launches the floor is set by co-located staked bots with a higher `edge_cap`; correct tip discipline therefore biases the *realized cohort* toward low-contention launches that may be residual/adverse. The §3 haircut is applied per-fill and does not model this cohort-composition bias. **Action: new condition C-3 — log live tip floor at decision time for every candidate and report GATE-A net PnL STRATIFIED by tip-contention bucket; if the only profitable cohort is the low-contention bucket, that is negative-selection residual, not edge, and scale-up (R4) is blocked.**

**1D. The 20–70ms budget describes internal compute only, not landing; a single latency number implies a landing race that does not exist. [MED] — ACCEPTED.**
**Action: `latency-budget.md` must separate internal compute (ingress→sign, the only thing 20–70ms describes) from the irreducible block-engine RTT + staked-lane/co-location gap to the LIVE (rotating) leader, name the expected extra-slot penalty for an unstaked non-co-located bundle, and propagate it as increased `buyers_ahead`/adverse-selection input — in plain numbers, not a footnote.** Architecture/infra subscriptions must be sized to "detection-competitive, submission-disadvantaged," never to landing competitiveness.

**1E. The five surfaces share two synthetic knobs (competitor-delay, exit haircut) → one fragile edge reported five ways, not five independent shots. [MED] — ACCEPTED.**
**Action: walk-forward §8 slippage-stress/placebo tests run with the CORRECTED competitor distribution and report how many surfaces survive INDEPENDENTLY. If edge survives only when surfaces are pooled, it is treated as one fragile edge, not a diversified portfolio, and the portfolio-robustness framing in §1 is struck.**

### Critique 2 — backtest-qa-engineer red-team (can the GATE-A/GATE-B methodology HONESTLY prove edge?)

**2A. GATE-B naive-momentum baseline is unbuildable (no price/volume series in `LaunchEvent`) and unpinned (K/percentile/unit-of-risk/universe chosen post-hoc → p-hackable). [HIGH] — ACCEPTED (verified).**
Confirmed: `types.py` `LaunchEvent` has no time series; grep for `momentum|buy_pressure|volume|tick` returns nothing. A baseline whose knobs are set after seeing model results is a tautology, not a control. **Action: condition C-4 — (i) add a first-K-slot buy-pressure/volume feature to the Data Contract so the baseline is constructible; (ii) FREEZE the baseline (K, percentile, unit-of-risk = net-PnL/SOL-at-risk or /downside-deviation, candidate universe) in a committed, hashed config BEFORE any model training, with a test that FAILS if baseline params change after the first model fit. Until frozen, GATE-B is unprovable.**

**2B. The methodology cannot detect a leak it doesn't already suspect: shuffle test is blind to (a) arrival-time vs block_time clock substitution and (b) a self-calibrated adverse-selection haircut fit on the same fills it's applied to. [HIGH] — ACCEPTED (verified the structural hole).**
The haircut as written in §3 ("realized slippage conditional on subsequent adverse move") conditions on post-event-time information and, if globally fit across the train/test boundary, is lookahead in a cost-model costume. **Action: condition C-5 — (i) add a block_time-vs-arrival-time clock audit: every feature snapshot ordered by slot/block_time, with a deliberately-shifted-clock control that MUST change results; (ii) fit the adverse-selection haircut ONLY on train-fold fills, freeze it, apply unchanged to test folds; any per-window re-fit is an auto-FAIL leak.**

**2C. Survivorship is asserted-away, not proven-absent: R1 shadow ingestion only records what it observed, so un-snapshottable fast rugs and dropped/dead launches are silently ABSENT — biased toward exactly EH-001's claimed cohort. [HIGH] — ACCEPTED (verified the hole).**
**Action: condition C-6 — recorded-data completeness audit: reconcile recorded launch count against an independent full pool-create census (second source), quantify and bound the miss rate; rows missing a completed first-K snapshot OR a resolved label are carried as explicit CENSORED/right-truncated outcomes, never dropped. "Survivorship-free" remains unproven until the miss rate is measured and bounded.**

**2D. The sim's profitability is circular (net PnL monotone in `model_skill`; blind gate-on is already +213.95 SOL because `safety.py:43` reads `truth_is_rug`/`truth_rug_detectable` at assumed `catch_rate=0.75`). Risk = this scaffolding leaking into the recorded harness. [HIGH] — ACCEPTED (verified).**
Confirmed `safety.py:43` reads ground truth at an assumed recall — precisely the quantity (catchable-rug recall ≥ 0.50) recorded data must PROVE. **Action: condition C-7 — the recorded-data validation harness is a CLEAN-ROOM rebuild with a static-analysis/import guard that FAILS the build if any recorded-gate code path references a simulator `truth_*` field or any path derived from `truth_max_multiple`; recall ≥ 0.50 must be MEASURED on held-out labeled rugs in test folds, never set as a parameter.**

**2E. Adverse selection modeled as an additive constant haircut cannot capture the conditional-fill mechanism (you get filled BECAUSE the trade is bad); and R2 no-submit venue experiences zero real fills, so the R2 haircut is an unvalidated prior — yet the ladder reads as if R2 GATE-A is dispositive. [HIGH] — ACCEPTED.**
**Action: condition C-8 — state explicitly that R2/GATE-A is NECESSARY-NOT-SUFFICIENT and the R2 adverse-selection number is an unvalidated prior; the FIRST real haircut validation is deferred to R3 realized fills. In the recorded harness, model fill-probability as CONDITIONAL on the outcome label (not independent) so the stress test perturbs the CORRELATION, not just the level.**

**2F. Multiple-testing defense is toothless — nothing COUNTS the trials; a one-sided lower-95% bound over 5 windows is cleared by a moderately-searched strategy with no deflation. [MED] — ACCEPTED.**
**Action: condition C-9 — a committed, append-only, hashed experiment log (every config/threshold/feature-set/profile-bucket/exit-mode evaluated) is a PRECONDITION for computing GATE-A/GATE-B; the significance deflation is a function of the logged count, not self-attestation. No log → auto-FAIL. Additionally the pass bar is raised: the lower bound must hold under the trial-count-deflated threshold AND survive shuffle/placebo AND regime-stratified reporting.**

**2G. Time-purge does not stop ACTOR leakage: same creator wallet / bundler cluster / deploy template recurring across the embargo boundary as different mints leaks identity. [MED] — ACCEPTED.**
**Action: condition C-10 — add group-aware purging by creator wallet / bundler cluster / deploy-template fingerprint across the embargo boundary; report fold metrics with AND without group-purge so the identity-memorization contribution is visible.**

### Critique 3 — independent quant red-team (is residual edge illusory net of full round-trip + adverse selection?)

**3A. EH-002 "most-proven lever" is the most-CIRCULAR: a TP ladder's edge is mechanically produced by synthetic path shape; on near-vertical real rugs the ladder may never reach rung-1 before LP pull — DIRECTION unproven, not just magnitude. [MED] — ACCEPTED.** Reinforces 1B. **Action: EH-002 demoted (below).**

**3B. The 75–150 bps adverse-selection haircut is an unmeasured placeholder; true thin-pool realized haircut can be ~300 bps, at which EH-001's +150..+400 midpoint collapses to breakeven-or-negative → every net-bps figure is currently unfalsifiable. [MED] — ACCEPTED.** Reinforces 1A/2E. **Action: condition C-11 — calibrate the haircut from recorded fills at R1 BEFORE GATE-A is computed at R2; gate R2 on the calibrated (not placeholder) haircut. Sub-gate: if calibrated haircut > 200 bps at target size, EH-001's net midpoint is re-derived and the surface re-justified or killed.**

**3C. EH-005 smart-money is behind-the-fill noise; its +0..80 bps top-of-band has no mechanism surviving entry lag; correct prior is expected-ZERO/negative. [LOW] — ACCEPTED.** **Action: EH-005 re-classified expected-ZERO (default dead); struck from any edge-supporting rationale (below).**

**3D. R2 recorded → R3 tiny-real is an unmodeled regime change: recorded fills contain NO market impact from the desk's own order; and a 2026 microstructure photograph rots in weeks. [LOW] — ACCEPTED.** **Action: condition C-12 — `capital-staging.md` states the R2→R3 market-impact caveat (recorded GATE-A/B is necessary-not-sufficient; R3 is a FRESH proof, not a continuation) AND a proof-staleness bound (if the regime-drift monitor flags a break between an R2 pass and R3 funding, or the passing window exceeds a stated freshness limit, the gate auto-re-runs on fresh recorded data before any lamport moves).**

### Rebuttals / scope limits (where I did NOT simply concede)

- **The verdict does not change to NO-GO and does not change to GO.** Every flaw above is a reason the edge is UNPROVEN net of cost — which is exactly what GO-PAPER-ONLY asserts and what the build is designed to test with real capital disabled by default. No critic argued the surfaces are *provably* negative; they argued the favorable numbers are sim artifacts. That is a mandate to prove on recorded data, not to halt and not to fund. **Verdict holds: GO-PAPER-ONLY.**
- **The sim's contaminated constants do not retroactively license anything.** Critics 2D/2E note the sim is admittedly circular. It always was — §1 and `AATS-BRIEF.md` §7.2 state it proves direction only and NEVER licenses capital. The genuine new risk is leakage of that scaffolding into the recorded harness, which is closed by C-7 (clean-room + import guard). I do not concede the sim was ever offered as evidence of magnitude.
- **EH-001/EH-003 are squeezed, not killed.** 1A/3B shrink their margins and may flip EH-001 at >200 bps haircut, but the kill is data-driven (C-11 sub-gate), not assumed now. Their pre-registered kill conditions already end them if the recorded cohort net PnL ≤ unselected/≤ 0. I decline to pre-emptively kill a surface the recorded gate is built to test — that would be the inverse error (over-pessimism asserting a result instead of measuring it).

### EH framing corrections (binding, applied to §2)

- **EH-002 — "most-proven lever" framing STRUCK.** In a sim with synthetic paths it is the most-CIRCULAR surface. Its DIRECTION (not just magnitude) is unproven until it clears the ≥ +10% net bar on RECORDED post-entry paths with the exit haircut modeling insider/LP-puller dump-into-sell, per C-5/1B. It is now labeled "exit discipline — direction unproven, most sim-circular surface."
- **EH-005 — re-classified expected-ZERO (default dead).** It must NOT appear anywhere as edge-supporting; it is a hypothesis to falsify, contrarian/adversarial by default. Its disconfirming default of +0 stands and its only legal wiring remains filter/de-risk (never a buy trigger or risk-increaser — a release blocker if violated).
- **§1 portfolio framing tempered.** The five surfaces are NOT five independent shots while they share the competitor-delay and exit-haircut knobs (1E); independence is reported by C-1, not assumed.

### FINAL DECISION

**GO-PAPER-ONLY (UNCHANGED). proceedToSpec = true.** Build the paper/shadow-record system; real capital stays disabled by default behind the DRY-RUN flag and is authorized by the CEO only after the recorded-data gates pass with the hardened conditions below. A recorded-gate failure remains a SUCCESSFUL outcome ("no edge net of cost"), not a project failure.

**Conditions for GO (all are blocking on the path to R3/real capital; failure to satisfy any is a release blocker, not a tuning choice):**

- **C-1 (latency honesty):** `latency-budget.md` states the solo floor as DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED in plain numbers — internal compute budget separated from block-engine RTT + staked-lane/co-location gap to the live leader, with a named extra-slot penalty propagated as increased `buyers_ahead`/adverse-selection input.
- **C-2 (no inherited optimism):** the R1/R2 recorded cost stack MUST NOT inherit `venue.py` `_competitor_delay()` or `exits.py` sandwich constants for line items it cannot directly observe; where the desk submits nothing, the haircut is derived from observed pool depth-decay and observed insider/LP-puller dump incidence, calibrated UPWARD until live R3 fills measure it. The 75–150 bps haircut is a FLOOR to widen, never a band to narrow.
- **C-3 (tip-cohort-bias kill):** log live tip floor at decision time per candidate; report GATE-A net PnL stratified by tip-contention bucket; if only the low-contention cohort is profitable, flag negative-selection residual and BLOCK scale-up.
- **C-4 (freeze + build the baseline):** add a first-K-slot buy-pressure/volume feature to the Data Contract; FREEZE the naive-momentum baseline (K, percentile, unit-of-risk, universe) in a committed hashed config before any model training; test FAILS if it changes after first fit.
- **C-5 (clock + frozen haircut):** block_time-vs-arrival-time clock audit with a shifted-clock control that must change results; fit the adverse-selection haircut ONLY on train-fold fills, freeze across test folds; per-window re-fit = auto-FAIL.
- **C-6 (completeness audit):** reconcile recorded launches against an independent pool-create census; bound the miss rate; carry un-snapshotted/un-labeled tokens as explicit censored outcomes.
- **C-7 (clean-room harness):** import/static-analysis guard FAILS the build if any recorded-gate path references a `truth_*` field or a path derived from `truth_max_multiple`; recall ≥ 0.50 MEASURED on held-out labeled rugs, never set as a parameter.
- **C-8 (R2 necessary-not-sufficient):** state it explicitly; first real haircut validation deferred to R3 fills; model fill-probability CONDITIONAL on outcome in the recorded harness so the stress test perturbs the correlation.
- **C-9 (experiment log + deflation):** committed append-only hashed experiment log as a precondition for GATE-A/GATE-B; significance deflation a function of logged trial count; no log → auto-FAIL; bound must survive deflated threshold + shuffle/placebo + regime-stratified reporting.
- **C-10 (group-purge):** group-aware purging by creator/bundler/deploy-template fingerprint across the embargo boundary; report metrics with and without group-purge.
- **C-11 (calibrated-haircut sub-gate):** calibrate haircut from recorded fills at R1 before computing GATE-A at R2; if calibrated haircut > 200 bps at target size, re-derive EH-001's net midpoint and re-justify or kill.
- **C-12 (regime + staleness):** `capital-staging.md` carries the R2→R3 own-order market-impact caveat (R3 is a fresh proof) and a proof-staleness bound that auto-re-runs the gate on fresh recorded data if drift breaks before funding.
- **C-13 (independent-surface reporting):** walk-forward §8 reports how many surfaces survive INDEPENDENTLY under the corrected competitor distribution; pooled-only survival is treated as one fragile edge, not a portfolio.

These conditions are handed to `quant-product-analyst` (G0 acceptance criteria), `solana-systems-architect` (architecture constraints — clean-room harness, Data Contract feature, latency-budget split), `ml-prediction-engineer`, and `backtest-qa-engineer` (enforces C-2/C-4/C-5/C-6/C-7/C-8/C-9/C-10/C-11/C-13). The two controls that matter most — the daily-loss circuit breaker (§5 K-0) and the model-vs-baseline metric (§4 GATE-B / §5 K-1) — survive unchanged and are now buttressed by a constructible, frozen baseline (C-4) and a clean-room, leakage-audited harness (C-7).

=== HANDOFF ===
FROM: quant-research-lead
TASK: P0 — finalize edge verdict after independent red-team review
STATUS: COMPLETE
DELIVERABLES: C:/dev/aats/.agency/01-specs/EDGE-VERDICT.md (appended "Red-team review and final verdict": 3 critiques, accept/rebut per fatal flaw, 13 blocking conditions, final decision)
SELF-CHECK: Verified every red-team code citation against source — venue.py _competitor_delay (15/60/20/5), colo_shred my_delay=ceil(29/400)=1 (TIED N+1), exits.py SECURE sandwich_p=0.08/loss=0.04, safety.py:43 reads truth_is_rug/truth_rug_detectable at catch_rate=0.75, tips.py:33 floor fallback, types.py LaunchEvent has no price/volume series. Re-fetched 2026 ground truth: SWQoS reserves 80% QUIC for staked nodes / ~83% staked first-block hit (Helius, Chorus One, Everstake) — confirms solo desk is submission-disadvantaged. Accepted all HIGH/MED flaws (most verified-true); rebutted only the over-reaches (verdict not moved to NO-GO/GO; sim never licensed capital; EH-001/003 squeezed not pre-killed). Demoted EH-002 framing and re-classified EH-005 to expected-ZERO. Verdict remains GO-PAPER-ONLY with 13 conditions.
RISKS: EH-001 may flip negative once the calibrated thin-pool haircut exceeds ~200 bps (C-11 sub-gate handles this); realizable edge may collapse to a single fragile surface if EH-002 fails on recorded paths (C-13 surfaces this). Both are measured at the gate, not assumed now.
NEEDS: quant-product-analyst to encode C-1..C-13 as G0 acceptance criteria; solana-systems-architect for the clean-room harness, Data Contract buy-pressure feature, and latency-budget split; backtest-qa-engineer to enforce the hardened walk-forward. No CEO decision required to proceed to spec (real capital remains CEO-gated at R3).
===============
