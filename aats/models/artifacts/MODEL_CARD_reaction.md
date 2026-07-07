# MODEL CARD — Reaction / Front-Run Strategy (edge-proof harness)

> **VALIDATION ARTIFACT, NOT A CAPITAL LICENSE.** This is the smart-money/KOL/whale SIGNAL ->
> reaction strategy scored by the offline GATE-A / GATE-B edge proof
> (`aats/backtest/reaction_harness.py`, `aats/backtest/run_edge_proof.py --strategy reaction`). It
> emits SELECTION masks + net-of-cost PnL on recorded data — **never a price, never a size, never a
> trade decision, no win-rate**. Real capital stays DISABLED behind DRY-RUN regardless of any number
> here. Any downstream use may only DE-RISK — never size up, widen a stop, or override a hard stop.

## Why this strategy exists (the pivot)
The on-chain LAUNCH-DATA edge is decisively falsified: the launch-instant snipe
(`outcome_harness.py`) AND the momentum-@60s entry (`momentum_harness.py`) both ran **NO-GO** on
thousands of real launches under realizable exits. The only remaining thesis with a real prior
(`.agency/04-plan/REACTION-CORPUS-SPEC.md`) is **front-running the predictable retail reaction to a
PROVEN signal** — a smart-money wallet buy / KOL call / whale buy at time T, entered just after T
and exited into the reaction. This harness is the leak-free proof of that thesis.

## What it consumes
`C:/aats_shadow/reaction_corpus.jsonl` — one JSON line per SIGNAL event (flat schema; the recorder
is the ingestion lane's). The **decision anchor is the on-chain `signal_block_time_ms`** (T-300a),
already in-record, so **no RPC resolution is needed**; a null anchor is CENSORED (fail-closed).

### Schema drift bridged — SPEC (DexScreener) vs the LIVE bonding-curve recorder
The `REACTION-CORPUS-SPEC.md` forward shape is the DexScreener/launch shape (`txns_m5`, per-mark
`liquidity_usd`). The **live recorder emits a pump.fun BONDING-CURVE shape instead**: forward key
`txns` (not `txns_m5`), **no per-mark `liquidity_usd`** (a bonding curve is not a DexScreener pool),
and the SIGNAL carries the curve reserves `vsol_lamports` / `vtok`. Feeding that live shape straight
into the DexScreener-liquidity realizable-exit model made **every mark null-liquidity => honeypot =>
uniform total loss** (a NO-GO that measured nothing, not the edge). The harness now **bridges both
shapes without weakening the outcome model** (`read_reaction_forward`):
- buy/sell pressure is read from `txns` OR `txns_m5` (either shape — fixes a silent-drift that
  zeroed pressure on the live corpus);
- a mark's exit liquidity is its OWN `liquidity_usd` when present, else a **bonding-curve liquidity
  proxy** from the signal's `vsol_lamports` (`bonding_curve_liquidity_usd` = ~2x the curve's SOL
  reserve in USD — the two-sided-pool liquidity the `min_liquidity_usd_floor` is calibrated against;
  the SOL/USD rate CANCELS in the slippage ratio, so the haircut is a pure pool-fraction);
- a record with **NEITHER** a per-mark liquidity NOR a usable `vsol_lamports` carries no exit-liquidity
  information and is **CENSORED (fail-closed)** — never silently resolved to a fabricated -100%. If
  liquidity is *universally* absent, every record is censored => 0 resolved => no metric (honest).

The proxy uses the SIGNAL-time reserve (a signal-time quantity — **not lookahead**, and it never
enters the DECISION, only the OUTCOME marks). Because a curve deepens as a token pumps, the
signal-time reserve UNDER-states later depth (slippage is if anything conservative on a winner); a
rug/dump shows up as the price path collapsing, handled by the walk. Result on the real corpus: the
baseline moved from a uniform −1.035/SOL total-loss artifact to a **real, price-path-driven** figure.

## The label / outcome (leak-free, co-owned with backtest-qa-engineer)
For each signal: enter at the **front-run-latency-haircut** `signal_price_sol` just after the signal,
then walk the **strictly-post-signal** `forward` marks through the SAME production `ExitEngine`
(`resolve_momentum_outcome` -> `walk_position`), realizable exit (liquidity impact + honeypot, with
the bonding-curve liquidity above), net of the ~6% round-trip cost stack. The realized outcome is a
pure function of the forward path — it never enters that signal's own decision.

## The model rule (FROZEN constants — MUST NOT be tuned against the corpus)
- **BASELINE (GATE-B control) = FOLLOW EVERY SIGNAL** — enter on every recorded signal at a fixed
  per-trade risk. No free numeric parameter.
- **MODEL = a WALK-FORWARD source-reputation filter.** Follow every signal EXCEPT decline one whose
  source has a **proven** track record (>= `min_prior_signals` *eligible* prior outcomes) whose
  **mean realized net-PnL-per-SOL** is **below `reputation_threshold`**. The model is a strict
  **SUBSET** of the baseline (it can only DE-RISK — drop proven-loser sources — never add a signal),
  so GATE-B measures exactly one thing: **does demoting proven-loser sources beat following everyone,
  net of cost, per unit risk?** If not, there is no model (it stays silent).

### Frozen params artifact (anti-p-hacking / audit parity)
`aats/models/artifacts/REACTION_PARAMS.frozen.json`, loaded at import (audit-parity twin of
`MOMENTUM_PARAMS.frozen.json` / `REALIZABLE_EXIT_PARAMS.frozen.json` / `baseline.frozen.json`).

| param | frozen value | meaning |
|---|---|---|
| `reputation_threshold` | `"0"` (Decimal) | net-PnL-per-SOL floor a proven source must clear; 0 = don't follow a proven net-loser (structural floor, not fitted) |
| `min_prior_signals` | `3` (int) | eligible prior outcomes before the reputation gate applies; below it a source is on the neutral prior |
| `neutral_prior_selects` | `true` (bool) | neutral-prior policy: an unproven source is followed (innocent-until-proven-loser) |
| `entry_latency_haircut_bps` | `50` (int) | front-run entry haircut: fill HIGHER than the signal tick by 0.5% (the reaction has already begun); conservative — only lowers PnL |

A canonical SHA-256 of `params` is pinned in `frozen_hash`; a test FAILS
(`reaction_params_changed_after_freeze`) if any value drifts. Changing a value is a **test failure,
not an edit** — open an ADR + delta notice.

## The two leak boundaries (both structurally enforced + tested RED-before/GREEN-after)
1. **DECISION vs OUTCOME (signal-anchored).** The decision reads ONLY signal-time features
   (`signal_price_sol`, `signal_size_sol`, the walk-forward `source_reputation`), each stamped at
   `signal_block_time_ms` and run through `assert_features_leq_decision` (cutoff = the signal anchor)
   BEFORE any decision. A feature derived from a `forward` mark carries event-time
   `signal_block_time + horizon_s*1000` > the cutoff and raises `LeakError`. Tests:
   `test_leak_b1_red_forward_feature_injected`, `test_leak_b1_red_forward_feature_via_assembler`
   (RED) / `test_leak_b1_green_signal_time_only_decision` (GREEN).
2. **WALK-FORWARD SOURCE REPUTATION.** The reputation used for signal *i* aggregates ONLY the SAME
   source's outcomes *j* with **`signal_block_time[j] < signal_block_time[i]`** (strictly prior — a
   source's own current signal and any future signal are excluded) AND
   **`outcome_resolved_time[j] <= signal_block_time[i]`** (the prior outcome was fully realized by
   the decision instant — no outcome-completion lookahead). `assert_reputation_inputs_prior` enforces
   BOTH structurally; injecting a source's current/future outcome, or a not-yet-observed prior
   outcome, raises `ReputationLeakError`. A source's first signals ride the **neutral prior** — no
   future outcome of the same source ever informs the current decision. Tests:
   `test_leak_b2_red_current_or_future_same_source_outcome`,
   `test_leak_b2_red_prior_outcome_not_yet_observed` (RED) /
   `test_leak_b2_green_strictly_prior_observed`,
   `test_leak_b2_end_to_end_reputation_ignores_future_same_source` (GREEN).

## GATE-A / GATE-B confidence interval — CLUSTERED / BLOCK bootstrap (not i.i.d.)
The shared gates (`gate_a` / `gate_b`) resample TRADES **i.i.d.**, which assumes every trade is an
independent draw. For the reaction corpus that is false on two coupling channels: signals **cluster
in time** (market-wide reaction bursts) and are **same-source-coupled** (the model's only lever is
per-source reputation). Under positive within-group correlation an i.i.d. bootstrap **understates
variance and can manufacture a `lower95 > 0` on correlated noise** — an in-sample edge that would
license capital on an artifact. The reaction runner therefore scores its lower-95 bound with a
**CLUSTER bootstrap** (`aats/backtest/reaction_gate.py`): it resamples WHOLE clusters and certifies a
pass only when the bound clears zero under the **conservative MIN** of a **SOURCE** clustering AND a
**TIME-BLOCK** clustering (`GATE_CLUSTER_SLOTS_PER_BLOCK`). This is strictly harder than i.i.d., so it
can only WITHHOLD a pass, never fabricate one; a clustering with `< 2` clusters cannot certify. The
**point estimates** (aggregate PnL, the delta, cohort figures) are the shared gates' verbatim (audit
parity) — only the CI + pass verdict are clustered. `run_edge_proof` reports
`gate_ci_method = clustered_bootstrap[source,time_block]` for the reaction strategy;
launch/momentum keep the i.i.d. bound. Proof: `test_source_cluster_withholds_where_iid_manufactures_a_pass`
(i.i.d. PASSES on one-cluster-concentrated delta, the cluster bootstrap WITHHOLDS).

> **Capital note (charter "in-sample edge is no edge"):** even a clustered GATE-B PASS on the whole
> corpus is only a FAST PRE-CHECK, not a capital license. The license bar is the purged/embargoed
> forward WALK-FORWARD below. Capital stays DISABLED regardless.

## Capital-licensing purged/embargoed WALK-FORWARD (the license bar)
The whole-corpus clustered bootstrap above is an IN-SAMPLE pre-check — it resamples the same window
it was measured on. A capital license requires the model to clear the bar OUT-OF-SAMPLE, forward in
time, with label-overlap leakage purged. `aats/backtest/licensing.py` (invoked by
`run_edge_proof --licensing` / `--walk-forward`) is that engine:
- decisions are ordered by their on-chain `event_time` and cut into `n_folds` (>= 5) contiguous
  forward TEST folds (the harness surfaces the aligned `outcome_event_times_ms` /
  `outcome_label_horizon_end_ms` for this);
- **PURGE** drops any decision whose label horizon resolves past its fold's end (its realized outcome
  depends on the next fold's price action — the label-overlap leak); the purge is load-bearing
  (`assert_purge_is_load_bearing`, tested non-vacuous);
- **EMBARGO** drops the boundary of each subsequent fold (serial correlation across the cut);
- the **SOURCE + TIME-BLOCK clustered** GATE-A / GATE-B are kept INSIDE every fold and on the POOLED
  out-of-sample union — the primary statistic. A GO requires >= 5 non-empty folds AND the pooled OOS
  GATE-A (model, absolute net-of-cost edge) AND GATE-B (delta, effective-sample-gated) to pass; the
  in-sample pre-check can additionally VETO. Every clause can only WITHHOLD a GO, never manufacture
  one. Proof: `tests/backtest/test_licensing.py` (a genuine time-spread edge licenses; a pure-loser
  corpus does not — the pooled GATE-A absolute-edge veto).

## Thin-cohort GATE-B floor (effective-sample gate)
GATE-B now gates on the EFFECTIVE decision cohort — the count of decisions where `model_selected !=
baseline_selected` (the only trades that move the delta; both-take trades cancel) — NOT total-n. Below
a frozen floor (`DEFAULT_EFFECTIVE_MIN_SAMPLE`, hard floor >= 21 so a caller cannot license the
~8-20-decision fragility band) `gate_b_pass` is forced False with a surfaced reason. For the reaction
model the effective cohort is exactly the signals the model DECLINED (`n_declined_by_model`), so a
GATE-B "PASS" riding a handful of declines is withheld — the fragility that reversed an n=497 edge at
n=4,187. Proof: `tests/models/test_gate_b.py::TestEffectiveSampleGuard`.

## Data-sufficiency caveat (why a delta of 0 is not "no skill")
The walk-forward reputation gate only fires once a source has `>= min_prior_signals` **observed**
prior outcomes. On a corpus with few repeat sources, almost every decision rides the **neutral
prior**, so **model == baseline by construction and GATE-B delta is exactly 0** — the selection
filter *never got to act*. That is NOT evidence that source selection has no skill; the reaction edge
is untestable until repeat-source density grows. The harness surfaces this as
`ReactionHarnessStats.reputation_engaged` and the runner prints a `DATA CAVEAT` line when the gate
never fired, so a null delta is not misread.

## Adversarial-hype note (contrarian rule)
No social / synchronicity feature enters the selection. The externally-provided `source_prior`
(caller-score / wallet reputation from the corpus) has **unverified point-in-time provenance**, so it
is carried for **AUDIT ONLY and is never a selection input** — the model relies solely on the
leak-free reputation it computes itself (`test_source_prior_does_not_change_selection`). A KOL-call
`signal_type` is treated identically to any other proven-actor signal: conviction comes from the
source's *realized* track record, never from the manufactured hype of the call itself.

## Known limitations / honest caveats
- **Front-running is competitive and the ~6% cost gate is brutal** — this may still be NO-GO. The
  maker does not grade the verdict; GATE-A / GATE-B on the real corpus do, and a NO-GO is the honest
  answer (no override).
- **Entry-latency haircut is a documented, non-fitted structural constant** (50 bps). A real bot
  fills later and worse than the signal tick; the haircut can only LOWER PnL (conservative — it can
  never fabricate a GO). It stacks with, and is distinct from, the cost stack's AMM entry slippage.
- **Reputation is MONEY, not a win-rate** — the mean of realized net-PnL-per-SOL ratios (exact
  Decimal). There is no win-rate field, target, or tuning objective anywhere (a test asserts the
  absence of the tokens).
- **Bonding-curve liquidity is a SIGNAL-TIME proxy.** The exit-liquidity depth is derived from the
  curve reserve at the signal instant (the corpus carries no per-horizon reserve). This is
  conservative on a winner (a pumping curve deepens, so later depth is under-stated) but can
  under-state slippage on a fast rug; the price-path collapse still drives the loss. Fail-safe: it
  can never fabricate a GO. If the recorder later emits per-mark `liquidity_usd`, that wins over the
  proxy automatically.
- **Corpus dependency.** v1 (whale-buy) is fully on-chain; v2 (smart-money-wallet / KOL) needs a
  curated wallet set / Telegram creds (flagged in the spec). The harness is validated against a
  hand-authored fixture in the interface shape until the recorder is flowing.

## Boundaries (non-waivable)
Outputs SELECTION + net-of-cost PnL for the edge proof — never a price, never a size, never a trade
decision. Offline / deterministic: no RPC (anchor in-record), no keypair, no signing, no OMS, no
capital. Reproducible: same corpus -> byte-identical `net_pnl_lamports`
(`test_reproducible_net_pnl`).
