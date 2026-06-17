# ADR-0013 — `SubmitMode.DEVNET`: a REAL devnet submit path that does NOT unlock mainnet LIVE

**Status:** Accepted (E1 frozen-contract delta MINOR-1) · **Date:** 2026-06-17
**Author:** `solana-systems-architect` (issued under the post-G1 ADR + delta-notice rule; only the
architect changes a frozen contract)
**Supersedes/relates:** execution-venue.md §1 / §1.1 / §4 (`SubmitMode` enum), infrastructure.md §6
(`devnet` env row), §9 (`SOLANA_CLUSTER` env), §10 (post-G1 changes). Relates to ADR-0009 (signer
caps still apply to `DEVNET` txs) and the capital-staging ladder (infrastructure.md §3, which `DEVNET`
is deliberately OUTSIDE).

## Context
E1 (engineering wave) added a real devnet **SUBMIT** path — the system can now build, sign, and *land*
a transaction on the Solana **devnet** cluster, paid in worthless airdropped devnet SOL, to exercise the
full `quote → build → sign → simulate → land → reconcile` lifecycle against a live cluster.

The frozen contracts contradicted this E1 reality, so they were a lie an engineer would build the wrong
thing against:

- **execution-venue.md `SubmitMode`** listed only `SIMULATION` / `DRY_RUN` / `LIVE`. There was no enum
  member for a real submit that is *not* mainnet `LIVE`. An engineer wiring E1's devnet submit had
  nowhere legal to put it — the options were to abuse `LIVE` (catastrophic: conflates a worthless-SOL
  devnet send with the one mode that touches mainnet capital and its CEO-auth gate) or to abuse
  `DRY_RUN` (a lie: `DRY_RUN`'s defining invariant is that **no bytes ever reach a block engine**, and
  E1 transmits).
- **infrastructure.md §6** declared the `devnet` env `LIVE_DRY_RUN` with venue
  `JitoJupiterVenue (no submit)` — literally "no submit," directly contradicting the E1 real-submit path.

The hard requirement driving the fix: a real submit path that lets us shake out wiring and measure real
landing/reconcile latency **must exist**, but it must be **structurally incapable** of being mistaken for
— or promoted into — a mainnet live-capital path. The dominant failure mode to architect against here is
a "devnet went green, flip it to mainnet" promotion that skips the mainnet gates (DRY-RUN flag + CEO auth
+ funded-wallet refusal + the R3 *fresh* fill proof). Devnet has no real fills, no market impact, and no
adverse selection, so a devnet pass proves wiring and latency only — never edge, and never live-readiness.

## Options
1. **Reuse `LIVE` for devnet, switch behavior on `SOLANA_CLUSTER`.** Zero new enum member. Rejected:
   collapses the worthless-SOL devnet submit and the mainnet capital submit into one mode, so the type
   system can no longer tell them apart — the CEO-auth / DRY-RUN-disabled / funded-wallet gates would
   have to be re-expressed as runtime cluster checks, exactly the kind of buried flag check §1 exists to
   forbid. A single bug in the cluster check sends real capital. This is the corner-cut: it trades a
   structural guarantee for a runtime branch on the most dangerous path in the system.
2. **Reuse `DRY_RUN` for devnet.** Rejected: breaks `DRY_RUN`'s defining invariant (no bytes reach a
   block engine; `land()` returns `submitted=False, reason="dry_run"`). E1 transmits, so labeling it
   `DRY_RUN` makes the CI-asserted "zero network sends while dry-run" invariant (infrastructure.md §2)
   either false or meaningless. A lie in the safest mode is worse than no mode.
3. **A new, additive `SubmitMode.DEVNET`, hard-gated on `SOLANA_CLUSTER=devnet`, OUTSIDE the staging
   ladder, that cannot promote to `LIVE`.** A strict superset of the contract: one new enum member, a
   cluster selector that pins endpoints + wallet + mode together, and an explicit "devnet does not
   advance the mainnet ladder" rule. Chosen.

## Decision
**Option 3.** Add `SubmitMode.DEVNET` to the enum (execution-venue.md §1) with the semantics frozen in
§1.1 and infrastructure.md §6:

- `DEVNET` is a **REAL submit** — `land()` transmits a real signed tx — but **only to the devnet
  cluster, paid in worthless airdrop SOL.**
- `DEVNET` is **bound to `SOLANA_CLUSTER=devnet`** (new env, default `mainnet`). The cluster selector
  pins the RPC/Geyser/block-engine endpoints, the airdroppable devnet wallet, and the `DEVNET` mode
  together. A venue on `SOLANA_CLUSTER=mainnet` **refuses to construct** in `DEVNET` mode
  (`devnet_mode_requires_devnet_cluster`). Conversely the mainnet live path is reachable only on
  `SOLANA_CLUSTER=mainnet`.
- `DEVNET` **does NOT and CANNOT unlock mainnet `LIVE`.** It is OUTSIDE the capital-staging ladder
  (infrastructure.md §3), a parallel mode on a different cluster — never a rung that advances it.
  Mainnet `LIVE` remains gated on the three unchanged independent gates: `submit_mode == LIVE`,
  `DRY_RUN_ENABLED=false` (explicit) + CEO auth (AC-060), and the `JitoJupiterVenue` funded-mainnet-wallet
  refusal. `DRY_RUN_ENABLED` stays `true` in the devnet env — it gates the **mainnet** capital path and
  is independent of devnet's worthless-SOL real submits.
- The **`aats-signer` caps (§5.2, ADR-0009) still apply** to every `DEVNET` tx: per-tx/aggregate SOL
  cap, full program-ID allowlist, value-moving-transfer pinning (a devnet tip-account set selected when
  `SOLANA_CLUSTER=devnet`; same pinning mechanism). A `DEVNET` submit cannot exceed the spend caps any
  more than a `LIVE` one.

This is **E1 delta MINOR-1**: additive (one enum member + one env selector), no existing contract
narrowed, the mainnet live gates untouched. No code ships under this ADR itself; it is the contract the
E1 tasks build to.

## Consequences
- (+) The E1 real-devnet-submit path now has a correct, honest home in the type system — it is neither
  a `LIVE` (mainnet-capital) lie nor a `DRY_RUN` (no-transmit) lie. The `DRY_RUN` "zero network sends"
  CI invariant stays true and meaningful.
- (+) "Devnet went green → flip to mainnet" is **structurally impossible**, not merely discouraged:
  `LIVE` requires `SOLANA_CLUSTER=mainnet` + DRY-RUN disabled + CEO auth + funded-wallet, none of which a
  devnet pass touches or satisfies. Devnet proves wiring/latency only.
- (+) Real end-to-end land/reconcile latency is now measurable against a live cluster before any mainnet
  lamport is at risk — strictly better shakeout than the old `(no submit)` devnet row gave.
- (+) Custody blast radius is unchanged: the signer re-validates `DEVNET` txs identically (ADR-0009
  caps + allowlist + tip-pinning hold on devnet).
- (−) One new enum member and a new `SOLANA_CLUSTER` env to wire, plus a devnet endpoint set and an
  airdropped devnet wallet to provision and keep funded (deploy + custody work). Accepted: small,
  bounded, and entirely on worthless SOL.
- (−) A post-G1 change to two frozen contracts (execution-venue.md, infrastructure.md), issued with this
  ADR + the delta notice in infrastructure.md §10. Accepted: the contracts were factually wrong about
  E1 ("no submit" when E1 submits), and the fix is a strict additive superset with a hard cluster gate.

## Affected tasks (delta notice)
- **T-327** — venue impl: add `DEVNET` to `SubmitMode`; `DEVNET` `land()` transmits on the devnet cluster;
  refuse to construct `DEVNET` unless `SOLANA_CLUSTER=devnet` (`devnet_mode_requires_devnet_cluster`).
- **T-352a** — `aats-signer`: select the devnet Jito tip-account set when `SOLANA_CLUSTER=devnet`; caps
  and pinning otherwise unchanged.
- **T-500** — deploy: wire `SOLANA_CLUSTER`, devnet RPC/Geyser/block-engine endpoints, and the airdrop
  wallet for the `devnet` env.
- **T-250** — scaffold / `.env.example`: add `SOLANA_CLUSTER` (default `mainnet`).
- **T-340 / T-341** — control plane: if `/api/mode` or `/api/health` surfaces `submit_mode`/cluster,
  echo `DEVNET` / `SOLANA_CLUSTER` (read-only; no new control capability — asymmetric trust untouched).
- **T-251** — custody: provision and keep funded the airdroppable devnet trade-only wallet.
