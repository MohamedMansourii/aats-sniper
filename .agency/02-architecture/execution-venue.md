# execution-venue.md — The Pluggable ExecutionVenue Seam + Venue/Program-ID Registry (T-202)

**Version:** 1.2.0 — **FROZEN** (E1 delta MINOR-1, ADR-0013: `SubmitMode.DEVNET` adds a REAL devnet
worthless-SOL submit path behind `SOLANA_CLUSTER=devnet` that does NOT unlock mainnet `LIVE`. Prior:
1.1.0 post-G1-red-team; ADR-0009 makes `sign()` cross to `aats-signer`; the
SimulationVenue must implement the 8 promoted members sim-native per code-reviewer R-01/R-02).
**Author:** `solana-systems-architect`
**Date:** 2026-06-16
**Status:** **The seam from `sol-sniper/sniper_sim/venue.py` is LAW.** The real venues drop in behind
it (the 8 promoted members implemented per impl). The loop core imports the **interface**, never a
concrete venue. After G1, only the `solana-systems-architect` changes the interface (ADR + delta notice).

**Companion:** `BLUEPRINT.md §3` (seam promotion), `data-models.md` (Intent/FillResult),
`latency-budget.md` (where each venue sits on the hot path), `infrastructure.md` (DRY-RUN flag).

---

## 1. The interface (preserve the sim's signature)

The sim's `ExecutionVenue` ABC has one method: `execute(intent, event) -> FillResult`. Production
expands it to the full lifecycle the brief names — **quote → build → sign → simulate → land →
reconcile** — but keeps `execute()` as the SNIPE-path entry so the loops are unchanged. Tip /
priority / CU / slippage are **first-class fields on the land call** (already true on `SwapIntent`),
not hidden.

```python
class ExecutionVenue(ABC):
    name: str                                   # registry id

    # --- SNIPE hot path (unchanged from the sim seam) ---
    @abstractmethod
    def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult: ...
        # build + sign + (simulate) + land in one call, returns FillResult (landed/slot_delay/
        # buyers_ahead/slippage/tip/priority). Tip/CU/slippage are on the intent — first-class.

    # --- FAST path (exits/survivors) ---
    @abstractmethod
    def exit(self, intent: ExitIntent | ReduceIntent, position: Position) -> FillResult: ...

    # --- lifecycle primitives (used by execute/exit; exposed for testing + sell-sim) ---
    @abstractmethod
    def quote(self, mint: str, side: Side, amount_base: int) -> Quote: ...
    @abstractmethod
    def build(self, intent: Intent) -> UnsignedTx: ...
    @abstractmethod
    def sign(self, tx: UnsignedTx, wallet_id: str) -> SignedTx: ...
        # CROSSES A PROCESS BOUNDARY (ADR-0009, red-team-2 must-fix #1). `sign()` does NOT hold the
        # key: it serializes the UnsignedTx and calls the separate `aats-signer` process over a local
        # Unix-domain socket, receiving signed bytes back. The hot-core venue holds only the PUBKEY.
        # `aats-signer` independently re-validates the tx (SOL-spend cap, full program-ID allowlist,
        # Jito-tip-account-pinned transfers — infrastructure.md §5.2) and may REFUSE
        # (raises SignerRefused with a machine reason); a refusal aborts the snipe/exit, never the key.
    @abstractmethod
    def simulate(self, tx: SignedTx) -> SimResult: ...               # simulateTransaction (CU, revert)
    @abstractmethod
    def land(self, tx: SignedTx, tip_lamports: int, cu_price: int) -> LandResult: ...
    @abstractmethod
    def reconcile(self, land: LandResult) -> FillResult: ...

    # --- venue state (DRY-RUN is a first-class state, not a flag check buried in code) ---
    @property
    @abstractmethod
    def submit_mode(self) -> SubmitMode: ...     # LIVE | DEVNET | DRY_RUN | SIMULATION
```

`SubmitMode` is the structural enforcement of the hard DRY-RUN rule (FR-039, AC-060):

```python
class SubmitMode(str, Enum):
    SIMULATION = "SIMULATION"   # SimulationVenue — no network ever
    DRY_RUN = "DRY_RUN"         # real venue: quote+build+sign, land() raises before any network send
    DEVNET = "DEVNET"           # REAL submit on devnet worthless-SOL only (SOLANA_CLUSTER=devnet);
                                # does NOT unlock mainnet LIVE — separate cluster, separate gate (E1 / ADR-0013)
    LIVE = "LIVE"               # real submit on MAINNET — ONLY when DRY_RUN_ENABLED=false + CEO auth
```

In `DRY_RUN`, `land()` performs everything up to and including `sign()` (for latency measurement) and
then **refuses to transmit** — it returns a `LandResult(submitted=False, reason="dry_run")`. There is
no code path in `DRY_RUN` that reaches the block engine. (FR-039.)

### 1.1 `DEVNET` — a REAL submit path on worthless SOL that does NOT unlock mainnet LIVE (E1, ADR-0013)

E1 added a real devnet SUBMIT path. `DEVNET` is **distinct from `DRY_RUN`**: in `DRY_RUN` no bytes ever
reach a block engine; in `DEVNET` `land()` **does transmit** a real, signed transaction — but **only to
the devnet cluster, paid in worthless devnet airdrop SOL.** Its purpose is wiring shakeout and end-to-end
landing/reconcile measurement against a real cluster *without risking one lamport of mainnet capital.*

The non-negotiable invariant (enforced structurally, audited at G1 / `crypto-security-engineer`):

- **`DEVNET` is gated on `SOLANA_CLUSTER=devnet` and is unreachable on mainnet.** The cluster is a hard
  config selector: the RPC/Geyser/block-engine endpoints, the airdroppable devnet wallet, and the
  `DEVNET` submit_mode are bound together. A venue running against `SOLANA_CLUSTER=mainnet` **cannot**
  be in `DEVNET` mode — the venue refuses to construct (`devnet_mode_requires_devnet_cluster`).
- **`DEVNET` does NOT and CANNOT promote to `LIVE`.** Passing/landing on devnet unlocks **nothing** on
  mainnet. Mainnet `LIVE` remains gated on the three independent gates unchanged: `submit_mode == LIVE`,
  `DRY_RUN_ENABLED=false` (explicit) + CEO auth (AC-060), and the `JitoJupiterVenue` funded-wallet
  refusal. `DEVNET` is a fourth, **parallel** mode on a different cluster — never a rung that advances
  the mainnet capital-staging ladder (infrastructure.md §3). The ladder's gates (R3 fresh proof, CEO
  auth) are mainnet-fill gates; devnet has no real adverse selection, no real impact, no real fills, and
  therefore proves wiring/latency only — never edge.
- **The signer-side caps still apply.** `aats-signer` (§5, ADR-0009) re-validates a `DEVNET` tx the same
  as any other: per-tx/aggregate SOL cap, full program allowlist, tip-recipient pinning. (A devnet tip
  account set is used when `SOLANA_CLUSTER=devnet`; the pinning mechanism is identical.) A `DEVNET`
  submit cannot exceed the spend caps any more than a `LIVE` one.

This makes `DEVNET` a strict, additive sibling of `DRY_RUN` (E1 frozen-contract delta MINOR-1): it adds
a real network submit *only on a worthless cluster behind an explicit cluster selector*, and the
type/config system makes "devnet success → mainnet live" impossible to express.

---

## 2. Concrete implementations behind the one interface

| Impl | Role | submit_mode | Notes |
|---|---|---|---|
| **`SimulationVenue`** | paper / replay (kept from sim) | `SIMULATION` | models landing race, slippage-with-co-buyers, min-out revert, exit sandwich. The R1/R2 cost stack MUST NOT inherit its `_competitor_delay`/sandwich constants (C-2; validation-harness.md). **Not "unchanged" literally (code-reviewer R-01):** the sim's `SimulationVenue` implements only `execute()`; the promoted ABC adds 8 members (`exit/quote/build/sign/simulate/land/reconcile/submit_mode`), so the promotion task (T-202/T-327) MUST implement them **sim-native** — `submit_mode=SIMULATION`, a no-network `land()` (returns a simulated `LandResult`), and `sign()` that does NOT cross to `aats-signer` (it returns a sim signature, no network, no key). Otherwise the class is abstract and uninstantiable. "Retained" means *its `execute()` semantics are preserved*, not *its body is untouched*. |
| **`JitoJupiterVenue`** | real production venue | `DRY_RUN` first, then `LIVE` | snipe buy = **direct AMM instruction** against decoded pool keys (FR-028, AC-018); exits/survivors = **Jupiter v6/Ultra** (`/order` managed or `/build` raw, then swap). Jito bundle `[buy, assert_min_out, tip_ix]` atomic (FR-040). Refuses to run without a funded isolated wallet + DRY-RUN explicitly disabled. |
| **`RaydiumVenue`** | direct Raydium AMM v4 / CPMM | `DRY_RUN` first | direct-AMM buy + sell against Raydium pool keys; a fallback exit path if Jupiter is unavailable (A-011). |
| **`DeadCcxtVenue`** | the CEX dead stub | n/a | **compiles, raises `NotImplementedError`** on every method. CEX is a NON-goal (SPEC §3); this exists only so the seam proves CEX *could* plug in without touching loop core. Never wired into a loop. |

**The loop core imports `ExecutionVenue` (the ABC), never a concrete class.** The active venue is
selected by config at startup and injected. Swapping `SimulationVenue` → `JitoJupiterVenue` is a
config change; the SNIPE/FAST loops are unchanged (the sim's promise, preserved). (ADR-0003.)

```python
# loop core — the ONLY import:
from aats.execution.venue import ExecutionVenue   # the interface
# NEVER: from aats.execution.jito import JitoJupiterVenue   # concrete — forbidden in loop core
```

---

## 3. The pluggable venue / program-ID registry (verified LIVE, never a stale ID in a hot path)

The single most dangerous bug in a Solana sniper is a **hardcoded, stale program ID** in a hot-path
decoder — pump.fun's migration target changed to PumpSwap (A-001), and IDs rotate. So program IDs are
**data, loaded and verified at startup**, never a literal in a decoder.

```python
class VenueRegistryEntry(BaseModel):
    venue: LaunchSource                 # pump.fun | pumpswap | raydium_v4 | raydium_cpmm | meteora | moonshot
    program_id: str                     # the on-chain program ID (NOT hardcoded in any decoder)
    decoder: str                        # decoder module name
    amm_fee_bps: int                    # PumpSwap 25 / Raydium 25 (A-005/006) — fee is registry data
    migration_target: LaunchSource | None  # pump.fun -> pumpswap (A-001)
    status: Literal["active", "candidate", "deprecated"]
    last_verified_slot: int             # set by the startup live-verify probe
```

### 3.1 Startup live-verification probe (FR-001, AC-002)

At startup (and on a periodic refresh), the registry runs a **live probe**: for each `active` entry it
confirms the program ID exists and is executable on-chain (account `executable=true`) and, where
possible, that a recent known pool decodes against it. An entry that fails verification is marked
`deprecated` and **removed from the hot path** — the snipe loop will not decode against an unverified
ID. The probe writes `last_verified_slot`. (ADR-0003.)

### 3.2 Build-time guard: no program ID literal in a hot-path file (AC-002, AC-014)

A static-analysis CI rule FAILS the build if a base58 program-ID-shaped literal (or a hardcoded Jito
tip integer) appears in any hot-path decoder or snipe-loop file. Program IDs come from the registry;
tips come from the live tip cache. This is the same guard family as the `truth_*` import guard
(validation-harness.md) — a build failure, not a runtime check.

### 3.3 Initial registry population (candidates verified at build time)

`active`: `pump.fun` (bonding curve), `pumpswap` (primary migration target, A-001), `raydium_v4`,
`raydium_cpmm`. `candidate` (behind the same registry, enrichment/discovery only until verified):
`meteora`, `moonshot`. Discovery enrichment (DEXScreener/Birdeye/Meteora/Moonshot, FR-006) is
SLOW-loop only and never gates the hot path; an enrichment source being down is not a halt (AC-007).

---

## 4. DRY-RUN / no-submit as a first-class venue state (the hard architecture constraint)

Real capital is DISABLED by default (BUILD-DIRECTIVE HARD RULE; AUTONOMY-DIRECTIVE non-waivable #1).
The DRY-RUN constraint is enforced at **three** structural levels, not one:

1. **Venue `submit_mode`** (§1): `DRY_RUN` `land()` cannot reach the network — there is no code path.
2. **`DRY_RUN_ENABLED` config flag** (`infrastructure.md`): `LIVE` mode is unreachable unless this is
   explicitly `false` (not absent) AND CEO auth is present (AC-060, api-contracts §5 `/api/mode`).
3. **`JitoJupiterVenue` refusal**: it raises unless a funded isolated trade-only wallet is configured
   AND DRY-RUN is disabled — you cannot fire it from the sim or from a default config (FR-039,
   preserving the sim stub's `NotImplementedError`-until-configured behavior).

Default startup mode is `SHADOW` (FR-004); the path from SHADOW → PAPER → LIVE_DRY_RUN → LIVE is the
capital-staging ladder, every step gated (api-contracts §2; infrastructure.md §staging).

**`DEVNET` is OUTSIDE this ladder (E1, ADR-0013).** It is not a rung between DRY-RUN and LIVE; it is a
parallel real-submit mode on the devnet cluster (`SOLANA_CLUSTER=devnet`, worthless airdrop SOL). It
does **not** satisfy any mainnet gate and **cannot** advance the staging ladder — devnet has no real
fills, impact, or adverse selection, so it proves wiring/latency only, never edge. The three mainnet
gates above (venue `submit_mode == LIVE`, the `DRY_RUN_ENABLED=false` flag + CEO auth, the funded-wallet
refusal) are unchanged and remain the ONLY path to mainnet capital. See §1.1.

---

## 5. Tip / priority / CU and slippage are first-class on the land call (not hidden)

Per the brief's mandate: the venue's `land()` takes `tip_lamports` and `cu_price` explicitly; the
intent carries `slippage_bps`. The tip is read from the **live** Jito tip cache (bundles-api-rest
`tip_floor` / `tip_stream`) off the hot path and bounded by `min(market_floor, 0.30×edge)` (FR-027;
`tips.py` invariant; hardcoded tip = build-FAIL AC-014). The `assert_min_out` instruction in the
bundle is the slippage enforcement: if slippage exceeds `intent.slippage_bps` the bundle reverts —
no tokens, no tip spent (FR-040; the sim's `reverted_min_out` behavior, made real).

---

## 6. Seam fidelity checklist (what an engineer must preserve)

- `execute(intent, event) -> FillResult` **arity and return type unchanged** from `venue.py`
  (code-reviewer R-02): the *signature shape* is preserved; the intent type is promoted
  `SwapIntent` → `EntryIntent` with integer/Decimal money fields (data-models.md §0/§6.2). "Unchanged"
  means arity + return, not the intent's internal field types — those move off `float` to money types.
- Loop core imports the ABC only (§2 code comment).
- `SimulationVenue` retained for paper/replay; **implement the 8 new ABC members sim-native**
  (`submit_mode=SIMULATION`, no-network `land()`, key-less `sign()`) so it stays instantiable
  (R-01) — its constants never bleed into the recorded cost stack (C-2).
- `JitoJupiterVenue` snipe buy = direct AMM (NOT Jupiter, FR-028); exits = Jupiter v6/Ultra.
- **`sign()` crosses to `aats-signer` over a local socket; the venue holds the pubkey, not the key**
  (§1, ADR-0009); the signer may refuse (caps + program allowlist + tip-pinning, infrastructure.md §5).
- `DeadCcxtVenue` compiles + raises (proves the seam, never wired).
- Program IDs from the live-verified registry; build fails on a hardcoded ID (AC-002).
- DRY-RUN is a venue state + a config flag + a venue refusal — three independent gates.
