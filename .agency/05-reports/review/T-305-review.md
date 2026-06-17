# Code Review — T-305 Buy-pressure / volume first-K-slot feature

**Reviewer:** code-reviewer (G3 quality half, paired with backtest-qa-engineer)
**Verdict:** **FAIL** (1 BLOCKER)
**One-line:** Clean, well-tested code that computes the wrong thing — the buy/sell
classifier does not match the data T-300's decoders emit, so the C-4 net-buy-pressure
feature collapses to total volume in production and the GATE-B baseline it exists to
enable is invalid.

---

## Verification (run by reviewer, not assumed)

| Command | Result |
|---|---|
| `python -m pytest tests/features -q` | 37 passed in 0.55s ✓ |
| `python -m ruff check aats/features/buy_pressure.py tests/features/test_buy_pressure.py` | All checks passed ✓ |
| `python -m mypy aats/features/buy_pressure.py tests/features/test_buy_pressure.py` | Success: no issues ✓ |

Engineer's lint/type/test claims reproduce exactly. The problem is not the engineer's
green bar — it is that the green bar encodes the same wrong assumption as the code.

---

## Findings

### BLOCKER B-1 — Buy/sell classifier is incompatible with the decoder output it must consume
`aats/features/buy_pressure.py:347-380` (`classify_event_direction`), exercised at
`:444`.

**What's wrong.** `classify_event_direction` assumes `sol_reserve_lamports` is a
*cumulative pool reserve* that grows on buys and shrinks on sells, and classifies
`event.sol_reserve_lamports >= pool_creation_event.sol_reserve_lamports → BUY`. The
upstream decoders (T-300, this task's declared dependency, `aats/ingestion/decoders.py`)
emit something entirely different:
- `_try_create` (`decoders.py:324`): `sol_reserve_lamports = 0` — the pool-creation
  event carries **zero** reserve.
- `_try_buy` (`:367`): `sol_reserve_lamports = max_sol_cost` — the *per-trade* SOL the
  buyer offered.
- `_try_sell` (`:410`): `sol_reserve_lamports = min_sol_output` — the *per-trade* SOL
  the seller received.
- PumpSwap `_try_swap` (`:590`): `sol_reserve_lamports = quote_amount` — again a
  *per-trade* amount.

These are per-trade flows, not a running reserve, and the create event's reserve is 0.

**Why it matters (empirically confirmed by the reviewer):** with the create event's
reserve = 0, every real trade has a positive per-trade `sol_reserve_lamports >= 0`, so
**every event classifies as a BUY**. Reproduction:
```
create reserve = 0
sell classified as buy?  True   (a real sell — should be False)
buy  classified as buy?  True
=> first_k_buy_count = 2, first_k_sell_count = 0
=> first_k_buy_pressure == first_k_volume_lamports  (always)
```
`first_k_sell_count` is structurally always 0 and `first_k_buy_pressure` is identical to
`first_k_volume_lamports`. The feature defined by data-models.md §3.2/§3.1 as "**net**
buy SOL pressure (buys − sells)" silently degrades to "total volume." Per the iron
rules and `data-models.md §3.2`, this exact feature exists *so the naive-momentum
baseline is constructible* ("enter every candidate with positive first-K-slot **net**
buy pressure above a fixed percentile", FR-015). A baseline built on this feature is not
measuring net pressure; it is measuring gross volume. Since the model-vs-baseline delta
is the project's acceptance metric, this corrupts the headline result rather than just a
single feature.

This is AATS review-brief item 4 (compute-time correctness of a feature) and a direct
contract-conformance failure against data-models.md §3.1/§3.2.

**Root cause is recoverable upstream, not by heuristic here.** The decoders *know* the
direction at decode time — pump.fun routes through `_try_buy`/`_try_sell` by
discriminator, and PumpSwap computes `is_buy`/`is_sell` (`decoders.py:562`). That ground
truth is then discarded: `LaunchEvent` (contracts/events.py) carries no direction field,
so T-305 is forced to re-derive what was already known and picks a heuristic that cannot
work on per-trade amounts.

**What good looks like.** Do not infer direction from a per-trade amount. Either:
(a) preferred — carry the decoder's known direction on the event (a `TradeDirection`
on `LaunchEvent`, or a separate decoded `TradeEvent` with `is_buy`), and have
`build_buy_pressure_features` read it; this needs a contracts delta and so is a scope
escalation to `solana-systems-architect` (the post-G1 change rule, charter §3.6); or
(b) if T-305 must stay self-contained for the C-4 baseline, accept pre-classified
`(event, is_buy)` pairs as input and make classification an explicit caller
responsibility — and the tests must then feed realistically-decoded directions, not a
fictional cumulative reserve. Until one of these is in place, the feature must not be
declared C-4-satisfying.

---

### MAJOR M-1 — No lower-bound on the source-slot window (pre-creation events accepted)
`aats/features/buy_pressure.py:232` (`observe` guard) and the cutoff property `:206-208`.

**What's wrong.** The guard rejects `event_slot > cutoff` but never checks the lower
bound. An event with `slot < event_time_slot` (before the pool existed) is accepted and
contributes to the feature. Confirmed:
```
pre-creation event (slot 990 < creation slot 1000) accepted; buy_count = 1; max_source_slot = 990
```
**Why it matters.** A stray earlier-slot event (mis-routed mint, reorg, replayed
stream) silently pollutes a point-in-time feature, and `max_source_slot` is then *below*
the creation slot — a nonsensical provenance window that the cutoff guard cannot catch
(it only checks the upper bound). For a module whose entire purpose is causal
cleanliness, the window should be closed on both ends.

**What good looks like.** Reject (or document-and-reject) `event_slot < event_time_slot`
in `observe`, with a sibling `FeatureWindowError`; add a test asserting a pre-creation
slot raises. If pre-creation events are ever legitimately expected, that decision belongs
in an ADR, not in silent acceptance.

---

### MINOR N-1 — Tests assert the model, but the model is the bug
`tests/features/test_buy_pressure.py:88-92` and throughout.

`POOL_CREATION_EVENT` uses `sol_reserve_lamports = 30 * LAMPORTS_PER_SOL` and the "buy"
fixtures use 35/40/45 SOL "reserves." No fixture uses the decoder's real output
(`create.sol_reserve_lamports = 0`, per-trade amounts for trades). The 37 tests are
internally consistent and genuinely assert behavior (not implementation) — but they
validate a cumulative-reserve world the pipeline never produces, which is why a
fundamentally broken classifier ships green. When B-1 is fixed, the fixtures must be
rebuilt from realistic decoded events (ideally a shared fixture derived from
`decoders.py` output) so the suite can actually fail on this class of defect.

---

### NIT — Direct private-attribute manipulation in a test
`tests/features/test_buy_pressure.py:249-260` pokes `acc._net_buy_lamports` etc. directly
to construct the cancellation scenario. It works and is clearly commented, but it couples
the test to internal field names; an `observe()`-based construction would be more robust.
Non-blocking.

---

## Conformance

| Check | Result | Note |
|---|---|---|
| Blueprint / data-models §3.1 field types | ✗ | Types match, but `first_k_buy_pressure` semantics (net) not delivered — see B-1 |
| Money rule (data-models §0) | ✓ | `first_k_buy_pressure` Decimal, `first_k_volume_lamports` int, no float anywhere |
| Provenance manifest (ADR-0010 §3.3) | ✓ | One `FeatureSourceWindow` per feature; `check_feature_window_cutoff` run at materialise; lineage `launch_events` |
| Point-in-time upper cutoff (guard 1) | ✓ | Future-slot events raise `FeatureWindowError`; inclusive boundary correct; atomic rejection proven |
| Point-in-time lower bound | ✗ | Pre-creation slots accepted — see M-1 |
| Batch == streaming parity | ✓ | Single accumulator implementation; parity tests meaningful |
| FeatureFrame schema conformance | ✓ | Populates `FeatureFrame`; float-rejection negative test present |
| Test presence & meaningfulness | partial | Tests assert behavior and pass, but encode the same wrong model as the code (N-1) |
| AATS brief item 3 (no float money) | ✓ | |
| AATS brief item 4 (no compute-time leak / feature correctness) | ✗ | B-1: feature does not compute its defined quantity on real data |
| Dependencies added outside blueprint | ✓ | None; pure stdlib + existing contracts |

---

## For the Orchestrator

FAIL on BLOCKER B-1. The fix touches the A→B seam: the correct repair (carry decoder
direction on the event) is a contracts change and should route to
`solana-systems-architect` for a data-models delta before `feature-quant-engineer`
re-implements; the self-contained alternative (caller supplies `is_buy`) keeps it in
M1 but requires realistic fixtures. Either way B-1 + M-1 + realistic fixtures (N-1) must
land before re-review. Pairs with `backtest-qa-engineer`; both must PASS for G3.
