# ADR-0015 — Go-live isolated signer is a separate PYTHON process; the un-bypassable enforcement contract

**Status:** Accepted (delta to ADR-0009 · resolves M3-audit RED-1) · **Date:** 2026-07-07 · **Author:** `solana-systems-architect`
**Delta to:** ADR-0009 (isolated signer = separate minimal-surface process). ADR-0009's *topology* is
**unchanged** — a separate process holds the secret, exposes only a loopback socket, and the hot core
holds only the pubkey. This ADR fixes the two things ADR-0009 left open and RED-1 caught as absent:
**(a) the go-live signer's language = Python**, and **(b) the byte-level enforcement contract** the
signer re-validates on every sign request. Issued with a **delta notice** (bottom) because ADR-0009 and
infrastructure.md §5 are frozen law.

## Context
- **RED-1 (Session V, M3 audit).** `rust/aats-signer/src/main.rs` is a **health-check-only scaffold**:
  no wallet secret, no signing, no Unix-domain socket listener — only `GET /health`. The ADR-0009
  un-bypassable enforcer (per-tx lamport cap + rolling/velocity aggregate + full program allowlist +
  tip-account pin) **does not exist anywhere**. The Python side has no independent out-of-process cap,
  and the production venue still defaults `signer_client` to `MockSignerClient()`. The single control
  ADR-0009 designates against unbounded loss is missing. This is a hard capital-gating blocker.
- **`cargo` is NOT available in this environment.** A Rust enforcer cannot be built *or tested* here.
  Shipping the RED-1 fix in Rust means shipping an un-testable binary — i.e. leaving the gap open while
  claiming it closed. That is the corner-cut we must not commit.
- **The production execution venue is already Python** (`JitoJupiterVenue`) and reaches the signer over
  a Unix-domain socket via `SocketSignerClient` (`aats/execution/signer_client.py`). The signer is a
  *peer process*; its language does not put anything in the Rust hot core's address space or under its
  GIL — the hot core pays only the socket round-trip, identical whether the peer is Rust or Python.

## Options
1. **Keep Rust, implement the enforcer in Rust now.** Strongest *eventual* attack surface (small static
   binary, no interpreter/stdlib, easy `mlock`/zeroize/seccomp). But `cargo` is unavailable → cannot
   build or test here → **RED-1 stays open**; and it re-implements tx parsing + per-venue spend decoders
   we already have and test in Python. Over-engineering the surface while corner-cutting the deliverable.
   **Rejected for now** (deferred, not cancelled — see Decision).
2. **In-process Python cap inside the venue.** Cheapest. **Rejected:** it lives in the caller's address
   space, so a compromised/buggy loop bypasses it — this violates ADR-0009's entire premise that the
   caller cannot be trusted to police itself. A cap the caller can edit is not a cap.
3. **Separate PYTHON signer process (`aats-signer`, Python impl).** Holds the secret, exposes only the
   loopback socket, **parses the tx bytes itself**, enforces the contract below, and **refuses (does not
   sign)** on any violation. Testable *now* with `pytest` (no `cargo`). Identical failure-domain
   isolation to ADR-0009 Option 3 — only the language differs.

## Decision
**Option 3.** The go-live isolated signer is a **separate PYTHON process**. ADR-0009's topology stands
verbatim (separate minimal-surface process, holds the secret via Vault-token→`mlock`→zeroize, loopback
Unix socket, hot core holds only the pubkey, no inbound network, no untrusted-byte decoding beyond the
one tx it must parse to police it).

**Rust is DEFERRED as a future minimal-attack-surface optimization — recorded why it is worth doing
later and why not now:**
- *Why later:* a Rust signer has no interpreter/stdlib in its address space, compiles to a small
  static/distroless binary, and makes `mlock`, memory-zeroize, and `seccomp` syscall-filtering of the
  secret-holding process materially easier — a real reduction of the memory-disclosure surface for the
  one process that holds the key.
- *Why not now:* `cargo` is absent (un-buildable, un-testable here), and shipping the RED-1 control
  today in a tested language beats shipping an un-testable stub. Crucially, **the Python enforcer's
  refusal-test suite becomes the byte-for-byte conformance spec the Rust port must pass**, so the port
  is low-risk and mechanical when the toolchain arrives. Deferring costs a marginally larger attack
  surface (the interpreter) on a process that already has no inbound network and no untrusted decode —
  an acceptable trade to CLOSE RED-1 with a proven control now.

**Latency honesty.** The `sign()` hop is a local Unix-socket round-trip budgeted ≤1.5 ms p99 in
ADR-0009. A Python signer adds interpreter + parse + ed25519 cost — realistically **a few ms, not
sub-ms**. That is a serial constant on the snipe path, but it sits **off the competitively-decisive part
of the race**: we do not win block-0 against co-located Jito-bundle shops regardless (BRIEF latency-floor
honesty), and a few ms of *local* signing does not change which co-buyer's bundle the leader includes —
slot timing and the network path to the block engine dominate. The custody guarantee is worth the few
ms; a Rust signer would reclaim them but not the race. A stated, deliberately-paid trade-off.

---

## The un-bypassable ENFORCEMENT CONTRACT (frozen)

**Trust model.** The signer holds the SECRET; the bot holds only the pubkey. On **every** sign request
the signer **trusts no caller-supplied field for policy** — it deserializes the transaction bytes itself
and re-derives every fact. `wallet_id` selects which key to use; `intent_kind`/`mint` in the request are
**logging hints only**, never policy inputs.

Checks run **in order**; the **first** failure → **refuse-not-sign** (return `{error, reason}`, emit **no
signature bytes**, do not mutate the velocity ledger). The signer signs **only** if C0–C5 all pass.

- **C0 — Parse.** Deserialize the versioned tx from `tx_b64`. Extract the static account-keys array, the
  payer (account 0 = the wallet), every instruction's `program_id` (resolved from the *static* keys by
  `program_id_index`), and each instruction's account indices + data. Unparseable → refuse
  (`signer_tx_unparseable`).
- **C0a — ALT security-account rule (fail-closed).** *v1 NON-goal:* security-relevant accounts resolved
  via an Address Lookup Table. If any instruction's `program_id_index`, or the **source/recipient of any
  System transfer**, resolves into the ALT-loaded region (index ≥ number of static keys), **refuse**
  (`signer_alt_unresolvable_security_account`). Program IDs and value-transfer accounts MUST live in the
  static keys so the signer can validate them from bytes with **no network** (it cannot fetch ALT
  contents). `tx_builder` already builds with `address_lookup_table_accounts=[]`; non-security accounts
  (pool vaults, etc.) may use ALTs later without touching this rule.
- **C1 — Program-ID allowlist (ADR-0009 refusal #2).** Every top-level instruction's `program_id` MUST
  be in the **live** allowlist set. Source: `config/program-allowlist.json` (live set = `core` + `active`
  venue + `router`, each verified `executable=true` at boot; `candidate` entries excluded). Any
  `program_id` not present → **refuse** (`signer_program_not_allowlisted`). **Default-deny.** The set is
  the venue programs **PLUS** SPL Token, SPL Token-2022 (only if emitted), ATA, ComputeBudget, System.
- **C2 — Value-transfer recipient pin (ADR-0009 refusal #3).** For every System-program instruction that
  moves lamports (`transfer` / `transferWithSeed` / `createAccount`-with-lamports) whose **source == the
  wallet**, the recipient MUST be in the pinned set:
  1. the **8 Jito tip accounts fetched from `getTipAccounts` at boot** and pinned (the JSON copy is a
     reference / fail-closed fallback only — the live-fetched set is authoritative; live keys differ from
     any hardcoded copy, so a stale literal is refused-by-diff, per `program-allowlist.json`
     `tip_accounts_authoritative_source`);
  2. the **wallet's own ATA(s)** — recomputed by the signer via
     `find_program_address([wallet, spl_token, mint], ata_program)` — for the wSOL-wrap account and the
     bought-token rent destination;
  3. the **wallet itself** (self-transfer no-op).
  Any other recipient → **refuse** (`signer_unpinned_transfer`). If `getTipAccounts` was unreachable at
  boot the pinned tip-set is **empty** → all tip transfers refuse (fail-closed); the signer never widens
  the pin to recover.
- **C3 — Per-tx SOL spend cap (ADR-0009 refusal #1a).** Compute wallet SOL outflow **from the tx bytes**:

  ```
  spend(tx) =  Σ lamports of every System transfer sourced from the wallet
               (captures Jito tip + ATA rent + the wSOL-wrap = the AMM buy amount for wSOL venues)
            +  Σ decoded native-SOL-debit of every top-level VENUE-program instruction
               (e.g. pump.fun bonding-curve buy `max_sol_cost`), read via the signer's per-venue
               spend-decoder table (config/signer-policy.json §venue_spend_decoders; 0 for pure
               wSOL-swap venues whose SOL already appears as a counted wrap transfer).
  ```

  **The rule that makes the cap un-bypassable:** if a top-level instruction targets a `venue`-category
  program and its discriminator/layout is **not** in the verified spend-decoder table (its SOL debit
  cannot be quantified from bytes), **refuse** (`signer_spend_undecodable`). You cannot hide spend in an
  instruction the signer cannot price, because such instructions are refused outright. Then: refuse if
  `spend(tx) > per_tx_cap_lamports` (100_000_000 = 0.1 SOL, OQ-005) → `signer_per_tx_cap_exceeded`.
- **C4 — Rolling-aggregate + velocity cap (ADR-0009 refusal #1b).** The signer keeps in-memory,
  monotonic-clock state `(t, spend)` for every signature it has **issued**, pruned to `window_seconds`.
  Refuse if `Σ in-window spend + spend(tx) > aggregate_cap_lamports` (500_000_000 = 0.5 SOL, OQ-005) →
  `signer_aggregate_cap_exceeded`; refuse if `in-window issued count + 1 > max_sign_count` →
  `signer_velocity_exceeded`. This is the **burst** blast-radius control — "cannot sweep the float in one
  or N signatures." (The *daily* 0.5 SOL tranche total is the circuit breaker's independent control, not
  the signer's.) On process restart the ledger starts empty (fresh window); the per-tx + aggregate caps
  still bound single-window outflow, and the wallet stays under-funded ≤ float as the out-of-band belt.
- **C5 — Approval hygiene.** Any SPL Token `Approve`/`ApproveChecked` with an unbounded/large amount to a
  delegate not in the venue allowlist → **refuse** (`signer_unbounded_approval`) — closes the
  standing-delegation drain (custody-policy on-chain hygiene, `program-allowlist.json` `spl_token_program`
  note).

**Why the caller cannot bypass (ADR-0009 refusal #4, structural, not a docstring):**
- The bot holds only the pubkey; the secret never leaves the signer process. A compromised/buggy loop can
  *request* a signature but cannot *produce* one.
- The signer re-derives every policy input from the tx bytes; lying in the request (`sol_in`,
  `intent_kind`, `mint`) changes nothing — those fields are never read for policy.
- The socket is loopback + file-permission + peer-credential gated (devops, T-352a).
- Refuse = **no signature bytes are ever emitted**; there is no "sign anyway" branch.

---

## Refusal exception + config source

**Refusal exception (existing type, extended reason set).**
`aats.execution.exceptions.SignerRefused(reason, message)` — unchanged class. The wire refusal is
`{"error": message, "reason": <code>}`; `SocketSignerClient` already maps it back to `SignerRefused`
(`signer_client.py`). Frozen reason-code set (a **delta** to the `SignerRefused` docstring — the engineer
transcribes it; the architect owns the taxonomy):

```
signer_tx_unparseable | signer_alt_unresolvable_security_account | signer_program_not_allowlisted |
signer_unpinned_transfer | signer_spend_undecodable | signer_per_tx_cap_exceeded |
signer_aggregate_cap_exceeded | signer_velocity_exceeded | signer_unbounded_approval | signer_unavailable
```

All are **refuse-not-sign** except `signer_unavailable` (socket down) — which the venue already maps and
which **aborts** the snipe/exit; it must **never** fall back to a mock signer. (RED-1 also removes the
`MockSignerClient` / `MockRpcClient` production defaults so a misconfigured LIVE fails loudly, not
silently to a mock — see delta notice.)

**Config sources — two files, both loaded by the signer at boot, both INDEPENDENT of the bot's RiskConfig.**
The signer MUST NOT import `aats.contracts.risk` — that would couple the enforcer to bot code and make the
cap mutable from the caller's side. The caps are duplicated deliberately (ADR-0009).
1. `config/program-allowlist.json` (**exists**, owner `crypto-security-engineer`) — C1 allowlist + C2
   tip/ATA pin. Boot-verified `executable`; `getTipAccounts` pinned.
2. `config/signer-policy.json` (**new, this ADR**) — C3 `per_tx_cap_lamports`, C4
   `aggregate_cap_lamports` / `window_seconds` / `max_sign_count`, and the `venue_spend_decoders` table
   for C3. Values authority: ADR-0009 / OQ-005 (0.1 / 0.5 SOL). **Tighten-only**: widening any cap is a
   config-file + deploy change, never a runtime POST.

---

## The exact enforcement interface for the execution engineer

> This is the contract to transcribe. The engineer implements the bodies in a **new** module
> `aats/execution/signer_enforcer.py` (built under the revised T-251 / RED-1) that the isolated Python
> signer process calls before every sign. Signatures + ordering are law; bodies are the engineer's.

```python
# aats/execution/signer_enforcer.py  (NEW)
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from aats.execution.exceptions import SignerRefused   # existing type; extended reason codes above

class SpendDecoder(Protocol):
    def max_sol_lamports(self, ix_data: bytes) -> int:
        """Upper bound (lamports) of NATIVE SOL this venue instruction can debit from the wallet.
        Return 0 for pure wSOL-swap instructions (their SOL is a counted System wrap transfer).
        RAISE (KeyError/ValueError) if the discriminator/layout is unknown -> caller REFUSES with
        signer_spend_undecodable. NEVER guess a bound."""
        ...

@dataclass(frozen=True)
class SignerPolicy:
    """Loaded ONCE at boot from config/signer-policy.json + config/program-allowlist.json.
    Immutable for the process lifetime. NEVER constructed from aats.contracts.risk."""
    per_tx_cap_lamports: int                          # 100_000_000  (0.1 SOL, OQ-005)
    aggregate_cap_lamports: int                       # 500_000_000  (0.5 SOL, OQ-005)
    window_seconds: int                               # C4 rolling window width (burst window)
    max_sign_count: int                               # C4 velocity: max issued sigs per window
    allowed_program_ids: frozenset[str]               # C1 live set (executable-verified at boot)
    venue_program_ids: frozenset[str]                 # subset of allowed that is category=venue
    pinned_tip_accounts: frozenset[str]               # C2 getTipAccounts@boot (empty => fail-closed)
    ata_program_id: str                               # C2 own-ATA derivation
    spl_token_program_id: str                         # C2 own-ATA derivation
    venue_spend_decoders: dict[str, SpendDecoder]     # C3 program_id -> decoder (missing => refuse)

class VelocityLedger:
    """In-memory, per signer process. monotonic-clock (t, spend_lamports) of ISSUED signatures,
    pruned to window_seconds. NOT persisted across restart (fresh window on boot, by design)."""
    def __init__(self, window_seconds: int) -> None: ...
    def would_exceed(self, now_s: float, spend_lamports: int,
                     aggregate_cap_lamports: int, max_sign_count: int) -> str | None:
        """Return 'signer_aggregate_cap_exceeded' | 'signer_velocity_exceeded' if adding this sign
        would breach either cap in-window; else None. Does NOT record (read-only check)."""
        ...
    def record(self, now_s: float, spend_lamports: int) -> None:
        """Call ONLY after a signature is actually issued (post C0-C5 pass)."""
        ...

class Enforcer:
    """The un-bypassable control. Constructed once with the boot-loaded policy + ledger.
    enforce() re-derives EVERY fact from tx_bytes; caller-supplied metadata is untrusted."""
    def __init__(self, policy: SignerPolicy, ledger: VelocityLedger) -> None: ...
    def enforce(self, tx_bytes: bytes, wallet_pubkey: str, now_s: float) -> None:
        """Run C0..C5 IN ORDER. Raise SignerRefused(reason=<code>, message=...) on the FIRST
        violation and return before any signing. Return None iff the tx MAY be signed. MUST NOT
        sign and MUST NOT mutate the ledger on refusal. The signer records the ledger AFTER it has
        produced the signature."""
        ...
```

```
# Signer request/response wire (UNCHANGED — aats/execution/signer_client.py):
#   request : 4-byte BE length + JSON {"wallet_id","tx_b64","intent_id","intent_kind","mint"}
#   success : JSON {"signed_b64","pubkey"}
#   refusal : JSON {"error","reason"}          # reason in the frozen set above
#
# Signer main loop (spec the engineer builds around Enforcer):
#   1. read request; select key by wallet_id            (secret NEVER leaves this process)
#   2. tx_bytes = b64decode(request["tx_b64"])
#   3. enforcer.enforce(tx_bytes, wallet_pubkey, monotonic())   # raises SignerRefused -> {error,reason}
#   4. signed = ed25519_sign(key, message_bytes(tx_bytes)); ledger.record(now, spend(tx))
#   5. return {"signed_b64": b64(signed), "pubkey": wallet_pubkey}
# Any exception path returns {"error","reason"} and signs NOTHING.
```

**Refusal tests the engineer must ship (the conformance spec, dual-G3 with `crypto-security-engineer`):**
over-cap tx → `signer_per_tx_cap_exceeded`; off-allowlist program → `signer_program_not_allowlisted`;
System transfer to a non-pinned recipient → `signer_unpinned_transfer`; venue instruction with an unknown
discriminator → `signer_spend_undecodable`; N-th sign in-window over aggregate/velocity →
`signer_aggregate_cap_exceeded` / `signer_velocity_exceeded`; caller lying in `sol_in`/`intent_kind` does
NOT change the verdict (policy re-derived from bytes); a valid in-budget tx signs.

---

## Consequences
- (+) **RED-1 closed with a tested control today**; the refusal suite is the executable spec, not a
  docstring promise.
- (+) Same blast-radius isolation as ADR-0009 — a hot-core RCE yields "what the signer will sign," capped
  at 0.1 SOL/tx and 0.5 SOL/window; only the language differs.
- (+) The Python enforcer's refusal suite **is the byte-for-byte conformance suite for the eventual Rust
  port** — the port becomes mechanical and low-risk.
- (−) Larger attack surface than a Rust binary (interpreter + stdlib) on the secret-holding process —
  accepted while it has no inbound network and no untrusted decode beyond the one tx it must parse.
  Revisit under a Rust-port ADR when `cargo` is in the toolchain.
- (−) A few ms added to the `sign()` hop vs Rust — off the decisive part of the race (stated above).
- (−) Two config files + a `venue_spend_decoders` table to keep in lockstep with venue IDLs. A venue IDL
  change that moves the buy discriminator makes that venue's buys **refuse** (`signer_spend_undecodable`,
  fail-closed, safe) until the decoder table is re-verified and redeployed — the same VERIFY-AT-BUILD
  discipline as the program-ID allowlist.

---

## Delta notice — affected board tasks
- **RED-1 (`.agency/verification/FORWARD-ROADMAP.md` item 1):** target changes from "Build
  `rust/aats-signer` (Rust)" to "Build `aats-signer` as a separate **PYTHON** process implementing this
  contract." The "**remove the `MockSignerClient` AND `MockRpcClient` production defaults**" clause
  **STANDS** unchanged.
- **T-251 (custody, `crypto-security-engineer`):** re-scope — enforcement module is now Python
  (`aats/execution/signer_enforcer.py`); add `config/signer-policy.json` to the audit scope. Secret
  handling (infrastructure.md §5.3: Vault-token → `mlock` → zeroize, never a static env var) is
  **unchanged**.
- **T-352a (`aats-signer` service, `latency-devops-engineer`):** the compose unit runs the Python signer
  (socket listener + peer-credential gate + Vault-token boot) instead of the Rust scaffold; the `/health`
  endpoint is retained. `rust/aats-signer` (health-check-only) is **demoted to "future minimal-surface
  optimization, non-blocking"** and must not be presented as the live enforcer.
- **T-327 (venue `sign()` seam):** wire contract unchanged; the production venue must **drop the
  `MockSignerClient` default** so a misconfigured LIVE fails loudly (RED-1).
- **`aats/execution/exceptions.py`:** extend the `SignerRefused` reason-code docstring to the frozen set
  above (engineer transcribes; taxonomy owned here).
- **New task (suggested to the Orchestrator):** build + dual-G3 the Python signer enforcer +
  `config/signer-policy.json` loader + the refusal test suite (`solana-execution-engineer` builds,
  `crypto-security-engineer` audits, `backtest-qa-engineer` + `code-reviewer` gate).
- **infrastructure.md §5.1/§5.2** wording ("the signer parses the *net SOL outflow*") is now **specified
  to byte-level** by this ADR (C3 with the fail-closed `signer_spend_undecodable` rule). No contradiction
  — a refinement of a frozen requirement.
