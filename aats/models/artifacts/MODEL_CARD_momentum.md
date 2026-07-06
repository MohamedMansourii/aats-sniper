# MODEL CARD — Momentum / Reaction-Entry Strategy (edge-proof harness)

> **VALIDATION ARTIFACT, NOT A CAPITAL LICENSE.** This is the momentum/reaction-entry strategy
> scored by the offline GATE-A / GATE-B edge proof (`aats/backtest/momentum_harness.py`,
> `aats/backtest/run_edge_proof.py --strategy momentum`). It emits SELECTION masks + net-of-cost
> PnL on recorded data — **never a price, never a size, never a trade decision, no win-rate**.
> Real capital stays DISABLED behind DRY-RUN regardless of any number here.

## What it is
Decide at `T_ENTRY` seconds after launch (default 60) from the early price/pressure trajectory
(the `<= T_ENTRY` marks), then hold to a later exit. Same leak-safe point-in-time machinery as the
launch harness; the decision boundary MOVES to `launch_block_time + T_ENTRY*1000`.

## The model rule (FROZEN selection constants — MUST NOT be tuned against the corpus)
Select iff, using ONLY the `<= T_ENTRY` trajectory:
1. **tradeable** at T_ENTRY (a non-null, positive price at the T_ENTRY mark), AND
2. **buy pressure** `buys / (buys + sells)` at T_ENTRY **>= `buy_fraction_floor`**, AND
3. **price rose** from the earliest pre-entry mark to the T_ENTRY mark (momentum confirmed), AND
4. **liquidity** `liquidity_usd` **> `min_liquidity_usd`** (a real, sellable market).

### Frozen params artifact (anti-p-hacking / audit parity)
The selection constants are **externalized** into a declared, change-controlled artifact —
`aats/models/artifacts/MOMENTUM_PARAMS.frozen.json` — loaded at import (the audit-parity twin of
`aats/models/baseline.frozen.json`). They are **NOT inline magic numbers**.

| param | frozen value | meaning |
|---|---|---|
| `buy_fraction_floor` | `"0.55"` (Decimal) | strictly more buyers than sellers by a clear margin at T_ENTRY |
| `min_liquidity_usd` | `"0"` (Decimal) | strictly-positive existence check — a real, sellable market must exist |

- Values are **Decimal-as-string, never float** (money/threshold discipline, data-models §0).
- They are declared **ONCE and FROZEN**; a canonical SHA-256 of `params` is pinned in
  `frozen_hash`. A test FAILS (`momentum_params_changed_after_fit`) if any value drifts.
- **These MUST NOT be tuned against the corpus.** Retuning a selection constant after seeing the
  scoreboard is p-hacking the control (red-team flaw 2A). Changing a value is a **test failure, not
  an edit** — open an ADR + delta notice instead.
- The externalized values are **byte-for-byte identical** to the constants they replaced (the
  previous inline `_MOMENTUM_BUY_FRACTION_FLOOR = 0.55` and the strictly-positive `> 0` liquidity
  existence check). This was an externalization, not a retune.

## The baseline (naive momentum — the GATE-B control)
Enter every launch that is **tradeable at T_ENTRY AND whose price rose to T_ENTRY** ("buy what is
going up"). It carries **no free numeric parameter** and is the strict SUPERSET of the model's
selection (the model adds the buy-pressure + liquidity quality filter). GATE-B therefore measures
exactly one thing: does the extra selectivity earn more net-of-cost PnL per unit risk than dumb
momentum? If not, there is no model (it stays silent).

## Known modeling limitations
- **~64s timing drift at T_ENTRY (mild latency-optimism).** The collector's forward marks are
  stamped at NOMINAL horizons (30/60/120/... s), but sampling drifts: the nominal-60s mark lands at
  **~64s median (p90 ≈ +15.6s)**. So the "reaction read at T_ENTRY" is really the state at
  `T_ENTRY + observation drift` — a few seconds MORE trajectory than a strict-live bot deciding at
  exactly T_ENTRY would have. This is a **mild latency-optimism**: a strict-live entry would see
  slightly LESS state, so the harness is, if anything, marginally advantaged. **We do not correct
  the collector** (that is the ingestion lane's fidelity fix — e.g. bonding-curve price at exactly
  T_ENTRY); we document it honestly so the drift is read as staleness, not lookahead. **The leak
  boundary is unaffected**: drift moves the mark later in real time, but its stamped event-time is
  still `<=` the decision cutoff, so `assert_features_leq_decision` still holds.
- **Off-grid T_ENTRY is a config error, now fail-loud.** `_entry_mark` requires an EXACT match on
  the corpus horizon grid `(30, 60, 120, 300, 600, 900, 1800)`. An off-grid `entry_horizon_s`
  (e.g. 45, 90) would silently find no T_ENTRY mark and decline every record (a misleading
  all-NO-GO). `_validate_entry_horizon` now raises `ValueError` instead.
- **DexScreener sparsity / horizon-compressed exit walk** — see the edge-proof report
  (`.agency/05-reports/qa/EDGE-PROOF-momentum-2026-07-06.md`): 93/497 unpriced at 60s (indexing
  lag), and the exit walk consumes coarse forward marks (coarse exit fidelity).
- **On-chain buy pressure can be wash-traded/bundled** — the buy-pressure feature is on-chain txn
  pressure, not manufactured sentiment; the caveat is documented, not modeled away. No social /
  synchronicity feature enters this model, so the contrarian-shilling rule has no surface here.

## Data hygiene
- **Malformed forward observations are CENSORED, not fatal.** A non-numeric `horizon_s` / price /
  liquidity in one observation is wrapped as `CorpusRecordError`, so that single record is dropped
  (consistent with existing censoring) rather than aborting the whole run.
- **Money discipline:** every PnL / risk magnitude is INTEGER lamports; prices are exact Decimal.
  No float money, no win-rate field anywhere (a test asserts the absence).

## Boundaries (non-waivable)
Outputs SELECTION + net-of-cost PnL for the edge proof — never a price, never a size, never a trade
decision. Offline / injectable: no live network, no keypair, no signing, no OMS, no capital. Any
downstream use may only DE-RISK — never size up, widen a stop, or override a hard stop.
