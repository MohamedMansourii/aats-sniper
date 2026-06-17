# ADR-0009 — Isolated signer is a SEPARATE minimal-surface process, not inside the Rust hot core

**Status:** Accepted (G1 red-team resolution) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`
**Supersedes:** the G1-candidate custody topology (signer in `aats-hotcore`) recorded in
BLUEPRINT §5.2 / infrastructure.md §5. Issued with a **delta notice** (BLUEPRINT §14) because the
topology was declared "law."

## Context
The crypto-security red-team (blast-radius lens: "when the hot core is owned, how much can one
signature drain?") found a high-severity custody flaw in the G1 candidate. The isolated signer was
placed **inside `aats-hotcore`** — the same process that does Geyser/ShredStream network ingest,
untrusted on-chain account-data decode, and block-engine submission. The private key therefore shared
an address space and failure domain with the most attacker-exposed code in the system. A poisoned
decode dependency or an RCE on the hot core would yield the **raw key**, not signing access bounded by
policy. Three further gaps: (a) the only signer-side refusal was a program-ID allowlist bound to the
*venue registry only* — under-inclusive (breaks legit txs that also carry SPL Token / ATA /
ComputeBudget / System) and over-trusting (no constraint on the System-transfer recipient); (b) no
signer-side SOL spend cap (the caps lived in RiskConfig, upstream of the signing boundary, so a
compromised loop could build an arbitrary-`sol_in` `EntryIntent` and the in-process signer would sign
it); (c) "Vault / env-injected" secret handling left an env-var path that puts the raw key in `/proc`
and core dumps.

## Options
1. **Keep the signer in the hot core, add policy checks.** Cheapest; preserves the lumped `build_sign`
   hop. But the key still shares the address space with untrusted-byte decoding and network egress — an
   RCE/dependency-poisoning still yields the raw key. Policy checks in the same process as the key are
   bypassable by the same compromise. Rejected: does not reduce blast radius.
2. **Hardware signer / HSM.** Strongest isolation, but heavyweight for a ≤2 SOL incinerable trade-only
   wallet at R3, and adds operational complexity disproportionate to v1. Deferred as a later option;
   the process-split below gives most of the blast-radius reduction now.
3. **Separate minimal-surface signer process (`aats-signer`) with independent signer-side policy.**
   The signer holds the secret in `mlock`-able memory (Vault short-lived token at boot, zeroized on
   exit), exposes ONLY a loopback Unix-domain `sign(unsigned_tx, wallet_id)`, has NO inbound network
   and NO untrusted-byte decoding, and independently re-validates every tx (SOL spend cap + full
   program allowlist + Jito-tip-account-pinned transfers). The hot core holds only the pubkey.

## Decision
**Option 3.** `aats-signer` is the only holder of the wallet secret and a separate failure domain. The
hot core builds the *unsigned* tx, calls the signer over a local socket, and submits the *signed*
bytes. The signer enforces three independent refusals (infrastructure.md §5.2):
1. **Per-tx + rolling-aggregate/velocity SOL spend cap** (0.1 / 0.5 SOL, OQ-005) — duplicated from
   RiskConfig deliberately, so a compromised loop cannot sweep the float in one or N signatures.
2. **Full enumerated program-ID allowlist** — venue-registry programs PLUS SPL Token, ATA,
   ComputeBudget, System.
3. **Value-moving-transfer pinning** — every System SOL-transfer recipient pinned to the 8 live-verified
   Jito tip accounts (`getTipAccounts` at boot — confirmed static set) + own ATA-rent destinations.

Secret handling is frozen (infrastructure.md §5.3): Vault short-lived token at boot, `mlock`, zeroize
on exit, **never a static env var**; the `.env` wallet field is a Vault reference only. The DMS's
pre-signed flattens are produced through `aats-signer`.

## Consequences
- (+) Blast radius of a hot-core compromise drops from "the key" to "what the signer's policy permits
  to be signed" — and the policy caps single-signature outflow at the per-trade floor.
- (+) The key is out of the address space that decodes untrusted on-chain bytes and talks to the
  network — the two highest-risk surfaces.
- (+) Signer-side caps are defense-in-depth: a compromised/buggy execution loop still cannot drain the
  float; the `<= float` guarantee no longer rests solely on out-of-band under-funding.
- (−) `sign()` now crosses a process boundary: a local Unix-socket round-trip on the snipe hot path,
  budgeted ≤1.5ms p99 added (latency-budget.md hop 5). A deliberate, stated cost paid for custody
  isolation — well inside the ≤150ms p99 SNIPE budget.
- (−) One more compose unit (`aats-signer`) and its socket/peer-cred hardening to build and audit
  (T-251 / T-352a). The execution-venue `sign()` seam changes from in-process to cross-process
  (T-327). Both listed in the delta notice (BLUEPRINT §14).
- (−) HSM-grade key isolation is still future work; the process split is the v1 floor, not the ceiling.
