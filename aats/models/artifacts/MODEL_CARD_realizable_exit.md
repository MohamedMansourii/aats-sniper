# MODEL CARD — Realizable-Exit Outcome Model (edge-proof fidelity layer)

> **OUTCOME-REALISM ARTIFACT, NOT A DECISION MODEL.** This layer changes only how a resolved
> trade's realized PnL is computed in the offline GATE-A / GATE-B edge proof
> (`aats/backtest/realizable_exit.py`). It emits **no probability, no price, no selection, no
> size, no trade decision, no win-rate**. It never touches the decision or the point-in-time
> leak boundary. Real capital stays DISABLED behind DRY-RUN regardless of any number here.

## Why it exists (the spot-optimism bug it closes)
The edge proof resolves each trade's OUTCOME by walking the forward DexScreener **spot** prices
through the production exit engine. Spot is **optimistic**: it silently assumes you can SELL a
fresh meme token AT the quoted mid, at full position size, at every mark. You cannot. Two
realities make the realized exit strictly worse than spot:

1. **Liquidity impact** — selling `per_trade_cap` of SOL into a thin pool moves the price against
   you; the fill is worse than spot by a slippage that grows with `notional / pool_liquidity`.
2. **Honeypot / unsellable** — a mark with `price_sol == null` (no market) OR liquidity below a
   real-market floor is a mark you cannot exit into at any size; a position never sellable in its
   hold window is a near-total LOSS (bag-held), not a gain at a price you could not realize.

Both launch and momentum strategies previously ran NO-GO on spot, and the momentum **baseline**
showed a positive mean (+4.58 SOL) that is a likely spot-optimism artifact. This layer makes
every future verdict trustworthy by refusing to book unrealizable exits.

## The model (conservative, capped, frozen)
On the OUTCOME (post-decision) marks a position is walked over:

- **Sellable mark** = priced (`price_sol > 0`) AND `liquidity_usd` present AND `>= min_liquidity_usd_floor`.
- **Liquidity slippage** (applied to the walk's spot proceeds):
  `slip = min(max_slippage_fraction, linear_impact_coeff * notional_usd / representative_liquidity)`,
  where `notional_usd = per_trade_cap_sol * sol_usd`, `sol_usd` is derived from the marks
  (`median(price_usd / price_sol)`, else the frozen `sol_usd_fallback`), and
  `representative_liquidity` is the **minimum sellable liquidity** in the hold window (the
  thinnest book you would realistically exit into — the conservative choice). This is a
  first-order (constant-product) linear price impact, hard-capped.
- **Honeypot** — if **no** mark is ever sellable (all unpriced OR sub-floor / null liquidity), the
  position recovers only `honeypot_residual_bps` of notional (= 0 -> a near-total loss minus fees).

Realized gross is then `spot_gross - deduction`, and the `~6%` round-trip cost stack
(`build_round_trip_cost_stack`) stacks **on top**.

### Frozen params artifact (anti-p-hacking / audit parity)
Constants are externalized into a declared, change-controlled artifact —
`aats/models/artifacts/REALIZABLE_EXIT_PARAMS.frozen.json` — loaded at import (the audit-parity
twin of `baseline.frozen.json` / `MOMENTUM_PARAMS.frozen.json`). They are **NOT inline magic
numbers**.

| param | frozen value | meaning |
|---|---|---|
| `linear_impact_coeff` | `"1.0"` | first-order price-impact coeff: slip = coeff · notional_usd / liquidity_usd |
| `max_slippage_fraction` | `"0.90"` | hard cap; a sellable exit always keeps ≥ 10% of spot proceeds |
| `min_liquidity_usd_floor` | `"500"` | null or sub-floor liquidity ⇒ UNSELLABLE (honeypot) |
| `honeypot_residual_bps` | `"0"` | never-sellable ⇒ total loss of notional (minus fees) |
| `sol_usd_fallback` | `"150"` | coarse SOL/USD used only when a mark lacks `price_usd` |

- Values are **Decimal-as-string, never float** (money/threshold discipline, data-models §0).
- Declared **ONCE and FROZEN**; a canonical SHA-256 of `params` is pinned in `frozen_hash`. A test
  FAILS (`realizable_exit_params_changed_after_freeze`) if any value drifts. Retuning a realism
  constant against the scoreboard is p-hacking the OUTCOME — a **test failure, not an edit**;
  open an ADR + delta notice.

## The conservative invariant (non-waivable)
For the **same** price path, **realizable net_pnl ≤ spot net_pnl** always — realism can only lower
PnL, never inflate it. Guaranteed **by construction**: `realizable_gross = spot_gross - deduction`
with `deduction = spot_proceeds - int(spot_proceeds * multiplier) ≥ 0` (`multiplier ∈ [0, 1]`,
`spot_proceeds ≥ 0`), plus a final clamp. The exit **schedule** (which rung fires when) is
unchanged — the exit engine still decides on the observed spot marks; only the realized fill is
haircut. Asserted on winner / flat / rug / thin / honeypot paths, on both harnesses, in
`tests/backtest/test_realizable_exit.py`.

## The toggle
`run_edge_proof(..., exit_model=...)` and `--exit-model {spot,realizable}` (default **realizable**).
`spot` is retained for parity/regression and to quantify the spot-optimism gap. The internal
`resolve_*` / `build_*` functions default to realizable too (the trustworthy default everywhere).

## Known modeling limitations
- **pump.fun bonding-curve liquidity is reported null by DexScreener** even though the token is
  curve-sellable. This layer, by design, treats null/sub-floor liquidity as **unsellable**
  (conservative — "never assume an exit you could not realize"). This will make many pre-migration
  pump.fun marks near-total losses; that is the intended conservative correction of spot-optimism,
  not a claim that every such token is a honeypot. A future fidelity fix (ingestion lane) could
  supply bonding-curve exit liquidity so curve-sellable marks are priced with curve slippage
  rather than the honeypot floor.
- **Representative liquidity = the minimum sellable book** in the window (worst realizable exit).
  This is deliberately conservative; a per-rung liquidity attribution (which rung sold into which
  book) would be more precise but requires per-step exit telemetry the walk does not surface.
- **Linear impact, capped** is a first-order approximation of constant-product slippage; it is
  conservative for small trades (0.1 SOL) into real pools, where the dominant realizable effect is
  the honeypot/unsellable treatment, not the (tiny) impact haircut.
- **On-chain liquidity can be faked/temporary** (wash liquidity, removable LP). The floor is a
  structural existence check, not a proof of durable exit liquidity; documented, not modeled away.

## Boundaries (non-waivable)
Outputs a realized net-of-cost PnL for the edge proof — never a price, size, probability, or trade
decision. It reads only OUTCOME marks (post-decision); it cannot touch the selection or the leak
boundary (a test asserts selection masks are identical under `spot` and `realizable`). Offline /
pure / deterministic: no network, no RNG, no keypair, no signing, no OMS, no capital. Any
downstream use may only DE-RISK — never size up, widen a stop, or override a hard stop.
