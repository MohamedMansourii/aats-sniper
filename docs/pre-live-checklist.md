# AATS — Pre-Live Checklist (before `DRY_RUN_ENABLED=false`)

**This is the gate between paper and real money.** Every item below must clear before
`DRY_RUN_ENABLED` is ever set to `false` and the trade wallet funded (rung **R3** of the
staging ladder). Until then, real capital is disabled and unreachable. Enabling live with any
item open is a **hard-rule violation**, not a configuration choice.

Three independent blocks must all be green:
- **A. Edge proven on RECORDED data** (GATE-A + GATE-B) — *the system must be shown to make
  money net of cost.*
- **B. Custody & security hardened** (COND-G4-2) — *a compromise must not be able to drain the
  wallet, and the supply chain must be trustworthy.*
- **C. CEO legal + funding authorization** (OQ-009 + R3 sign-off) — *the one decision the agency
  does not make alone.*

Current status of this build: **A = NOT MET (no recorded data); B = NOT MET (signer scaffold +
image/supply-chain gaps); C = NOT GIVEN.** Real capital stays disabled. This is the correct,
honest paper-deliverable state.

---

## A. Edge proven on RECORDED data

The acceptance metrics are net-of-cost PnL and model-vs-baseline — **no win-rate, ever**. Both
gates must pass on **recorded mainnet data** (never synthetic), with a lower 95% bootstrap bound
> 0, over purged/embargoed walk-forward windows. The harness is **built and proven to compute
correctly** in this build (`aats/models/gate_a.py`, `aats/models/gate_b.py`, `tests/validation/`),
but there is **no recorded data**, so neither gate can be — or is — passed yet.

- [ ] **R1 recording complete.** ≥ ~3,000 mainnet launches recorded in `SHADOW` mode with
      point-in-time first-K-slot features + event-time outcome labels; submit nothing.
- [ ] **Completeness / survivorship bounded** (C-6). Recorded launches reconciled against an
      independent pool-create census; miss rate measured and bounded; un-snapshotted tokens carried
      as explicit censored outcomes, never dropped.
- [ ] **Leak / clock audit clean** (C-5, C-7). Event-time-only joins; no `truth_*` field and no
      simulator import in any recorded-gate path (clean-room build guard); a deliberately-shifted
      clock control must change results. *(Guards proven non-vacuous at G4; re-run on recorded data.)*
- [ ] **Baseline frozen** (C-4). The naive-momentum baseline (K, percentile, unit-of-risk,
      universe) frozen in a committed hashed config **before** model training; a test fails if it
      changes after the first fit.
- [ ] **Adverse-selection haircut calibrated** (C-11) from recorded fills, widen-only from the
      150 bps floor. If the calibrated haircut > 200 bps at target size, EH-001 is re-derived and
      re-justified or killed.
- [ ] **Experiment log + trial-count deflation** (C-9). Committed append-only hashed log of every
      config/threshold/feature-set evaluated; significance deflated by the logged count. No log →
      auto-fail.
- [ ] **Group-aware purge** (C-10) by creator / bundler / deploy-template fingerprint across the
      embargo boundary; metrics reported with and without group-purge.
- [ ] **GATE-A PASS on recorded data.** Net-of-cost PnL aggregate, lower 95% bound > 0, over the
      walk-forward test windows.
- [ ] **GATE-B PASS on recorded data.** Model net-PnL-per-unit-risk beats the frozen baseline,
      lower 95% bound > 0, on the same windows.
- [ ] **Tip-contention stratification** (C-3). GATE-A net PnL reported by tip-contention bucket;
      if only the low-contention cohort is profitable, that is negative selection — scale-up blocked.
- [ ] **Independent-surface report** (C-13). How many edge surfaces survive *independently* under
      the corrected competitor distribution; pooled-only survival is treated as one fragile edge.

> If A fails, the correct deliverable is the finding **"no edge net of cost"** — a successful
> project outcome, not a failure. Do not proceed to fund.

---

## B. Custody & security hardened (COND-G4-2)

These are **latent today** only because LIVE is unreachable (DRY-RUN default + 3 gates + unfunded
wallet). They are **hard blockers** before `DRY_RUN_ENABLED=false`. Source of truth:
`.agency/05-reports/security/G4-security-audit.md`, `.agency/05-reports/gates/G4-PASS.md §2.2`.

### Signer-side custody enforcement (F-01, HIGH) — the wallet cannot be drained

The Rust signer (`rust/aats-signer/src/main.rs`) is currently a **scaffold**. The three refusals
are specified and the data is present (`config/program-allowlist.json`), but **no running code
enforces them**. The ≤-float blast-radius guarantee does not exist in code yet.

- [ ] **Spend-cap refusal built + test-proven.** Signer parses net SOL outflow and **refuses** an
      over-cap tx (per-tx `PER_TRADE_CAP_LAMPORTS` 0.1 SOL; rolling `MAX_AGGREGATE_LAMPORTS`
      0.5 SOL; velocity cap). Integer-lamport math, never float. Prove by test it refuses an
      over-cap tx.
- [ ] **Program-ID allowlist refusal built + test-proven.** Signer refuses any instruction whose
      program ID is off the full allowlist (System, ComputeBudget, SPL Token, ATA + verified venue
      programs + Jupiter v6). Prove by test it refuses an off-allowlist program ID.
- [ ] **Value-transfer pinning built.** Every System SOL transfer recipient pinned to the closed
      set (8 live `getTipAccounts`-at-boot Jito tip accounts + own ATA-rent destinations); any
      other recipient refused.
- [ ] **Secret handling built.** Wallet secret fetched via short-lived Vault token at boot, held
      in `mlock`-able memory, zeroized on exit; never in any env var / log / disk plaintext.
- [ ] **Peer-cred gate** on the Unix socket; the signer has no inbound network.

### Image & host hardening (F-10 / F-07, devops)

- [ ] **Real image digests** (F-10). `docker/Dockerfile.signer` (and Prometheus/Grafana/Alertmanager
      `@sha256:placeholder` pins) replaced with **real** digests. A placeholder cannot ship live.
- [ ] **Operator: restore real verified base-image digests** (F-10, operator action). For each base
      image run `docker pull <image>` then `docker inspect --format '{{index .RepoDigests 0}}' <image>`
      and pin the resulting `<image>@sha256:<digest>` across **all 7** `docker/Dockerfile.*`
      (`bot`, `controlplane`, `dashboard`, `dms`, `hotcore`, `signer`, `telegram`) **and**
      `docker-compose.yml` before `DRY_RUN_ENABLED=false`. Cross-ref the F-10 digest item above; see
      `docs/operator-onboarding.md §7`.
- [ ] **Signer container locked down** (F-07). `cap_drop:[ALL]` + `cap_add:[IPC_LOCK]` (required for
      `mlock`), `no-new-privileges`, read-only rootfs + tmpfs `/run`, socket-only/isolated network.

### Supply chain (F-02 / F-03 / F-04)

- [ ] **Hash-locked dependencies** (F-02). Generate a hash-pinned lock and install with
      `--require-hashes` in CI and the image build (`pyproject.toml` floats `>=` today).
- [ ] **Dependency CVE scan in CI** (F-03). Add `pip-audit` / `osv-scanner` as a CI gate and run
      it once with network to triage the current pins. (CVE posture is **INCONCLUSIVE** in the
      offline build — not "clean.")
- [ ] **GitHub Actions pinned to SHAs** (F-04), not tags.

### Re-verify the secret-clean property on the real commit

- [ ] Re-run the secret sweep on the committed tree + history (the audited tree was largely
      untracked working-tree files; CI history-depth scanning only has meaning once history exists).

---

## C. CEO legal + funding authorization

These are CEO decisions; the agency does not make them.

- [ ] **Legal confirmation (OQ-009).** The CEO confirms they have taken independent legal advice
      for their jurisdiction and that operating the bot for personal trading is legal and compliant.
      > **Disclaimer:** AATS makes **no** legal representation. An automated trading bot may be
      > subject to financial regulations (e.g. MiFID II, CFTC/SEC, or local market-maker/trading-bot
      > rules) depending on jurisdiction. Real-capital operation requires the operator's own legal
      > review and confirmation of compliance with local law. R3 authorization is withheld until this
      > is given.
- [ ] **Funding policy confirmed.** A dedicated **trade-only, capped, incinerable** wallet
      (`WALLET_MAX_BALANCE_LAMPORTS` ≤ 2 SOL at R3), **never** main holdings, topped up out-of-band
      in small tranches from cold storage that never signs through this system.
- [ ] **Risk floors set at funding time.** `DAILY_LOSS_LIMIT_SOL`, per-trade / aggregate caps —
      tightened (never loosened) for the live tranche.
- [ ] **R3 sign-off recorded.** Explicit CEO authorization to advance to R3 (tiny-real), captured
      as the `NEEDS-CEO-DECISION` it is.

---

## Flipping the switch (only when A + B + C are all green)

1. Set `DRY_RUN_ENABLED=false` **explicitly** (absent ≠ false), `AATS_ENV=mainnet-live`.
2. Fund the capped trade-only wallet; confirm `WALLET_MAX_BALANCE_LAMPORTS`.
3. Bring the signer up with a fresh Vault token; confirm it refuses an over-cap and an
   off-allowlist tx (the F-01 proof).
4. `POST /api/mode {LIVE}` with the CEO auth token (requires `DRY_RUN_ENABLED=false` + token).
5. Watch GATE-A / GATE-B live on Grafana over the first ≥ 100 trades / ≥ 2 windows. **R3 is a
   FRESH proof, not a continuation of R2** — the desk's own order has market impact recorded data
   never modeled. Any failed gate → revert. Any breaker-trip pathology → halt and review.

> **R3 → R4 (scale)** is its own gate: each size step requires a fresh passing walk-forward window
> at the new size. Slippage and adverse selection scale with size — re-prove, never extrapolate.
