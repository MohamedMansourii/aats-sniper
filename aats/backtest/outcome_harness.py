"""Outcome-labeling harness (Phase-5 unblock) — recorded launches -> TradeOutcome records.

THE ONE MISSING PIECE
=====================
The edge proof (GATE-A / GATE-B) returned NO-GO only because ZERO resolved
`aats.models.gate_b.TradeOutcome` records existed.  This harness produces them from the
recorded corpus (`labeled_corpus.jsonl`) so `run_edge_proof` can emit the FIRST honest
GATE-A / GATE-B verdict with real scoreboard numbers.

THE LEAK BOUNDARY (the whole game — point-in-time correctness)
=============================================================
The DECISION and the OUTCOME are STRUCTURALLY SEPARATED:

  * the DECISION (`model_selected` / `baseline_selected`) is a pure function of an
    `EntryRecord` — ENTRY-TIME data ONLY (the launch economics + the on-chain block_time
    resolved from the entry signature via getTransaction).  It is assembled into a set of
    point-in-time features, EACH stamped at the decision block_time, and the set is run
    through `assert_features_leq_decision` BEFORE any decision is computed.  A feature whose
    event-time is AFTER the decision (i.e. a forward observation) makes that guard RAISE.
    A single forward value touching the decision = LeakError = FAIL.

  * the OUTCOME (`net_pnl_lamports`) is a pure function of the entry price (entry-time) and
    the FORWARD price observations (strictly after the decision).  The forward marks are
    walked through the SAME production `ExitEngine` (`aats.risk.exit_sim.walk_position`) and
    the realized PnL is taken NET of the full ~6% round-trip cost stack via the cost model.

The two never share an argument: `decide_entry(entry)` cannot see the forward path, and
`resolve_outcome(entry, forward_path)` cannot change the selection.  `entry.recv_wall_ms`
(a COLLECTION timestamp) is NEVER a decision field — the decision anchors on the on-chain
block_time (T-300a); `recv_wall_ms` is retained for monitoring only.

REUSE (no forked trading logic)
===============================
  * SELECTION baseline: `aats.models.baseline.evaluate_baseline` (the FROZEN naive-momentum
    control) — verbatim, on a thin first-K proxy built from the entry's own initial buy.
  * EXIT / paper-walk: `aats.risk.exit_sim.walk_position` (which threads the production
    `ExitEngine.on_tick`) — verbatim, over the forward price path.
  * COST: `aats.contracts.intents.CostStack` + `aats.risk.cost_model.CostAwareEntryGate`
    (the round-trip cost stack + its coherence/floor validation).

THE MODEL DECISION IS THIN (documented limitation)
==================================================
The live `labeled_corpus.jsonl` carries only THIN PumpPortal entry features (initial buy,
bonding-curve virtual reserves, market cap, pool) — it does NOT carry the first-K-slot
microstructure / holder-concentration / authority-renounce features the trained ONNX snipe
classifier (`aats.models.inference`) requires.  So `model_selected` here is a DOCUMENTED,
clearly-labeled THIN-FEATURE decision (see `thin_model_selected`), NOT the trained
classifier.  The deliverable is REAL `TradeOutcome` records + a runnable edge proof, not a
perfect model — when the corpus grows the full first-K feature frame, swap the thin decision
for `SnipeInference` at the marked seam (`THIN_MODEL_SEAM`).

MONEY DISCIPLINE (data-models §0)
=================================
Every PnL / risk magnitude is INTEGER lamports (or exact Decimal for prices).  There is NO
float money in a `TradeOutcome`, and NO win-rate anywhere.

OFFLINE / INJECTABLE
====================
The forward-price source and the block_time resolver are injected Protocols, so tests use
fixtures with NO live network.  No keypair, no signing, no OMS, no capital.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from aats.contracts.events import EventTime
from aats.contracts.features import (
    FeatureProvenance,
    FeatureSourceWindow,
)
from aats.contracts.intents import CostStack
from aats.contracts.risk import RiskConfig
from aats.features.buy_pressure import BuyPressureFeatures
from aats.models.baseline import BaselineParams, evaluate_baseline, load_params
from aats.models.gate_b import TradeOutcome
from aats.risk.cost_model import CostAwareEntryGate
from aats.risk.exit_engine import ExitEngine, preset
from aats.risk.exit_sim import walk_position

LAMPORTS_PER_SOL = 1_000_000_000

# Deterministic global seed for the paper-walk sandwich draws.  Combined with a STABLE
# per-mint hash (sha256, never the salted builtin hash) so the harness is reproducible
# across runs / machines / PYTHONHASHSEED.
_WALK_SEED = 20260706

# The FROZEN feature window K the baseline was frozen for (must match baseline.frozen.json).
_FIRST_K_SLOTS = 10


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LeakError(AssertionError):
    """Raised when a decision feature's event-time is AFTER the decision block_time.

    This is the harness's structural leak boundary: it fires the instant a forward
    observation (or any post-decision datum) is offered to the decision step.
    """


class BlockTimeUnavailable(RuntimeError):
    """Raised by a block_time resolver when the entry's on-chain block_time cannot be
    resolved (tx not found / no blockTime).  Such a record is CENSORED (dropped), never
    given a fabricated anchor — fail closed (T-300a)."""


class CorpusRecordError(ValueError):
    """Raised when a corpus line is structurally unusable (missing/!-numeric entry
    fields, no forward observations).  The record is CENSORED, never defaulted."""


# ---------------------------------------------------------------------------
# Injected Protocols (offline-testable seams)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockTime:
    """On-chain event-time resolved from an entry signature (getTransaction)."""

    slot: int
    block_time_ms: int


class BlockTimeResolver(Protocol):
    """Resolve an entry signature -> on-chain (slot, block_time_ms).

    Production impl calls RPC getTransaction (block_time only, T-300a).  Tests inject a
    fixture mapping.  `entry.recv_wall_ms` is NEVER used to build this — it is a collection
    timestamp, not an on-chain event-time.
    """

    def resolve(self, signature: str) -> BlockTime: ...


class ForwardPriceSource(Protocol):
    """Produce the forward price observations for a corpus record.

    Default impl (`EmbeddedForwardPriceSource`) reads the `forward` list already present in
    each corpus line.  The Protocol exists so a live forward-fetch (Birdeye / DexScreener /
    RPC) can be injected without a network in tests.
    """

    def forward_path(self, record: CorpusRecord) -> list[ForwardObservation]: ...


class FixtureBlockTimeResolver:
    """Test/offline resolver: a fixed signature -> BlockTime mapping (NO network)."""

    def __init__(self, mapping: dict[str, BlockTime]) -> None:
        self._mapping = dict(mapping)

    def resolve(self, signature: str) -> BlockTime:
        bt = self._mapping.get(signature)
        if bt is None:
            raise BlockTimeUnavailable(
                f"no fixture block_time for signature {signature[:12]}... (CENSORED)"
            )
        return bt


class RpcBlockTimeResolver:
    """Production resolver: on-chain block_time via RPC getTransaction (T-300a).

    The RPC URL comes from the environment ONLY (never logged, never committed).  Returns
    the on-chain slot + block_time (ms); raises BlockTimeUnavailable when the transaction
    is not found or carries no blockTime (that record is CENSORED, never fabricated).

    This path is NOT exercised by the offline test suite (which injects the fixture
    resolver); it exists so an operator with RPC_PRIMARY set can resolve the real corpus.
    """

    def __init__(self, rpc_url: str) -> None:
        if not rpc_url:
            raise ValueError(
                "RpcBlockTimeResolver requires an RPC URL (set RPC_PRIMARY in the env). "
                "No URL is ever hard-coded or logged."
            )
        self._url = rpc_url

    def resolve(self, signature: str) -> BlockTime:
        import urllib.request

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {"encoding": "json", "maxSupportedTransactionVersion": 0},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (env-provided URL)
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network/parse failure -> CENSORED, never fabricate
            raise BlockTimeUnavailable(
                f"getTransaction failed for {signature[:12]}... (CENSORED)"
            ) from exc
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BlockTimeUnavailable(
                f"getTransaction returned no result for {signature[:12]}... (CENSORED)"
            )
        block_time_s = result.get("blockTime")
        slot = result.get("slot")
        if block_time_s is None or slot is None:
            raise BlockTimeUnavailable(
                f"getTransaction result missing blockTime/slot for {signature[:12]}... "
                "(CENSORED — T-300a: on-chain block_time is mandatory)"
            )
        return BlockTime(slot=int(slot), block_time_ms=int(block_time_s) * 1000)


# ---------------------------------------------------------------------------
# Structurally-separated records: ENTRY (decision-time) vs FORWARD (outcome-time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusRecord:
    """One raw corpus line: the entry dict + the forward observation list.

    This is the un-split container.  `to_entry_record` (with a resolved block_time) yields
    the ENTRY-ONLY view; `EmbeddedForwardPriceSource` yields the FORWARD-ONLY view.  The two
    views never share a field beyond the mint (an identifier, never a feature).
    """

    entry: dict
    forward: list[dict]

    @property
    def mint(self) -> str:
        return str(self.entry.get("mint", ""))

    @property
    def signature(self) -> str:
        return str(self.entry.get("signature", ""))


@dataclass(frozen=True)
class EntryRecord:
    """ENTRY-TIME view of a launch — the ONLY input the decision may read.

    Carries the launch economics (exact money) + the on-chain decision anchor.  It carries
    NO forward observation.  `recv_wall_ms` is retained for monitoring ONLY and is NEVER a
    decision field (T-300a).
    """

    mint: str
    decision_slot: int
    decision_block_time_ms: int
    # exact money / quantities (parsed from the corpus floats via Decimal(str(...)))
    sol_amount_lamports: int  # the entry (initial) buy SOL, in integer lamports
    v_sol_lamports: int  # bonding-curve virtual SOL reserve, integer lamports
    v_tokens_base: Decimal  # bonding-curve virtual token reserve (base units, exact)
    market_cap_lamports: int  # launch market cap in lamports
    initial_buy_tokens: Decimal  # tokens minted to the initial buyer (exact)
    pool: str
    recv_wall_ms: int  # COLLECTION timestamp — monitoring only, NEVER a decision field

    @property
    def entry_price_sol(self) -> Decimal:
        """Entry-implied price per token in SOL = v_sol / v_tokens (entry-time only)."""
        if self.v_tokens_base <= 0:
            raise CorpusRecordError(f"{self.mint}: non-positive v_tokens (CENSORED)")
        return (Decimal(self.v_sol_lamports) / Decimal(LAMPORTS_PER_SOL)) / self.v_tokens_base


@dataclass(frozen=True)
class ForwardObservation:
    """One FORWARD-TIME price observation — the ONLY input the outcome may read.

    `price_sol is None` means NO market at that horizon (rugged / dead) — a VALID outcome
    (worthless), not missing data.  Carries no entry economics.
    """

    horizon_s: int
    price_sol: Decimal | None  # exact (parsed from the corpus string), or None = no market
    liquidity_usd: Decimal | None
    dex: str
    note: str
    obs_wall_ms: int  # observation wall-clock — monitoring only

    @property
    def forward_event_block_time_ms_after(self) -> int:
        """Event-time offset of this observation AFTER the decision, in ms (= horizon_s)."""
        return self.horizon_s * 1000


class EmbeddedForwardPriceSource:
    """Default forward-price source: read the `forward` list embedded in the corpus line."""

    def forward_path(self, record: CorpusRecord) -> list[ForwardObservation]:
        out: list[ForwardObservation] = []
        for obs in record.forward:
            price_raw = obs.get("price_sol")
            liq_raw = obs.get("liquidity_usd")
            out.append(
                ForwardObservation(
                    horizon_s=int(obs["horizon_s"]),
                    price_sol=(Decimal(str(price_raw)) if price_raw is not None else None),
                    liquidity_usd=(Decimal(str(liq_raw)) if liq_raw is not None else None),
                    dex=str(obs.get("dex", "")),
                    note=str(obs.get("note", "")),
                    obs_wall_ms=int(obs.get("obs_wall_ms", 0)),
                )
            )
        # Order by horizon (event-time order) — the walk consumes marks in order.
        out.sort(key=lambda o: o.horizon_s)
        return out


# ---------------------------------------------------------------------------
# Point-in-time decision features + the LEAK GUARD (structural boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PITFeature:
    """A point-in-time decision feature: a value STAMPED at its source event-time.

    The decision is computed ONLY from a set of these, and the set is run through
    `assert_features_leq_decision` first.  A feature whose `event_block_time_ms` is after
    the decision anchor is a forward-looking leak and is refused.
    """

    name: str
    value: float
    event_block_time_ms: int


def assemble_decision_features(
    entry: EntryRecord, *, extra_features: tuple[PITFeature, ...] = ()
) -> list[PITFeature]:
    """Assemble the ENTRY-ONLY decision features, each stamped at the decision block_time.

    `extra_features` is the extension seam for future decision features.  ANY feature added
    here MUST be entry-time (event_block_time_ms <= decision) — the guard enforces it.  A
    forward observation injected through this seam is caught by `assert_features_leq_decision`
    (this is exactly what the leak test exercises).
    """
    bt = entry.decision_block_time_ms
    feats = [
        PITFeature("sol_amount_lamports", float(entry.sol_amount_lamports), bt),
        PITFeature("v_sol_lamports", float(entry.v_sol_lamports), bt),
        PITFeature("v_tokens_base", float(entry.v_tokens_base), bt),
        PITFeature("market_cap_lamports", float(entry.market_cap_lamports), bt),
        PITFeature("initial_buy_tokens", float(entry.initial_buy_tokens), bt),
    ]
    feats.extend(extra_features)
    return feats


def assert_features_leq_decision(
    features: list[PITFeature], decision_block_time_ms: int
) -> str:
    """LEAK AUDIT: assert EVERY decision feature's event-time <= the decision block_time.

    Mirrors `aats.models.training.assert_event_time_leq_decision`.  Returns a human-readable
    audit summary; raises `LeakError` on the FIRST feature whose event-time is after the
    decision (a forward-looking leak).
    """
    if not features:
        raise LeakError("leak audit: zero decision features (nothing to decide on)")
    for f in features:
        if f.event_block_time_ms > decision_block_time_ms:
            raise LeakError(
                f"LEAK: decision feature {f.name!r} has event-time "
                f"{f.event_block_time_ms} ms > decision block_time {decision_block_time_ms} ms. "
                "A post-decision (forward) datum reached the decision step. Boundary breached."
            )
    return (
        f"LEAK AUDIT PASS: {len(features)} decision features, all with "
        f"event_block_time_ms <= decision block_time ({decision_block_time_ms}). "
        "No forward datum reached the decision."
    )


# ---------------------------------------------------------------------------
# The DECISION step (ENTRY-TIME ONLY) — model + frozen baseline selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionResult:
    """The entry-time decision: two SELECTION masks + the fixed unit of risk.

    NO price, NO forward value.  `sol_at_risk_lamports` is a FIXED per-trade risk unit (the
    per-trade cap) so the two cohorts are compared on identical exposure — GATE-B then
    isolates SELECTION skill, not sizing.
    """

    model_selected: bool
    baseline_selected: bool
    sol_at_risk_lamports: int
    # audit-only (never a TradeOutcome field)
    baseline_momentum_lamports: int
    dev_buy_fraction: float
    leak_audit_summary: str


def _thin_buy_pressure_features(entry: EntryRecord) -> BuyPressureFeatures:
    """Build a THIN first-K buy-pressure proxy from the entry's own initial buy.

    The frozen naive baseline (`evaluate_baseline`) needs `first_k_buy_pressure` (Decimal)
    and `first_k_volume_lamports` (int) at K = frozen `first_k_slots`.  The thin corpus has
    only the deployer's initial buy, so we use it as the first-K net buy pressure / volume.
    This is a DOCUMENTED thin proxy; when the corpus grows a real first-K window, feed
    `build_buy_pressure_features` instead.  Point-in-time: max_source_slot = decision_slot
    (entry-time; never after the decision).
    """
    et = EventTime(
        slot=entry.decision_slot,
        block_time_ms=entry.decision_block_time_ms,
        wall_clock_ms=entry.decision_block_time_ms,
    )
    windows = [
        FeatureSourceWindow(
            feature_name=name,
            max_source_slot=entry.decision_slot,
            lineage_dataset="launch_events",
        )
        for name in ("first_k_buy_pressure", "first_k_volume_lamports")
    ]
    return BuyPressureFeatures(
        first_k_buy_pressure=Decimal(entry.sol_amount_lamports),
        first_k_volume_lamports=entry.sol_amount_lamports,
        first_k_buy_count=1,
        first_k_sell_count=0,
        first_k_unique_buyers=1,
        max_source_slot=entry.decision_slot,
        event_time=et,
        first_k_slots=_FIRST_K_SLOTS,
        provenance=FeatureProvenance(windows=windows, builder_version="thin-harness-1.0.0"),
    )


# --- THIN_MODEL_SEAM -------------------------------------------------------
# The trained ONNX snipe classifier (`aats.models.inference.SnipeInference`) plugs in HERE
# once the corpus carries the full first-K FeatureFrame.  Until then this is a documented,
# clearly-labeled thin-feature decision — entry-time only, distinct from the baseline, and
# contrarian by construction (an outsized deployer buy LOWERS conviction, never raises it).
# ---------------------------------------------------------------------------

# pump.fun bonding curves seed a ~30-SOL VIRTUAL reserve at t0; a curve meaningfully above
# it has taken real early inflow.  Illustrative thin threshold (NOT a tuned model param).
_THIN_VSOL_FLOOR = Decimal("31")
_THIN_MAX_DEV_FRACTION = Decimal("0.35")


def thin_model_selected(values: dict[str, float]) -> tuple[bool, float]:
    """DOCUMENTED THIN model decision from the guarded entry features (entry-time only).

    Selects iff the curve shows real early inflow beyond the virtual-reserve floor AND the
    deployer's own buy is NOT an outsized share of the curve (contrarian risk filter: a dev
    that bought most of the curve is a red flag, never a bullish signal — adversarial-hype
    rule).  Returns (selected, dev_buy_fraction).  This is NOT the trained classifier.
    """
    v_sol_lamports = Decimal(str(values["v_sol_lamports"]))
    sol_amount_lamports = Decimal(str(values["sol_amount_lamports"]))
    v_sol = v_sol_lamports / Decimal(LAMPORTS_PER_SOL)
    dev_fraction = (
        (sol_amount_lamports / v_sol_lamports) if v_sol_lamports > 0 else Decimal(1)
    )
    selected = (v_sol >= _THIN_VSOL_FLOOR) and (dev_fraction <= _THIN_MAX_DEV_FRACTION)
    return bool(selected), float(dev_fraction)


def decide_entry(
    entry: EntryRecord,
    *,
    baseline_params: BaselineParams,
    risk_config: RiskConfig,
    extra_features: tuple[PITFeature, ...] = (),
) -> DecisionResult:
    """Decide model + frozen-baseline SELECTION from ENTRY-TIME data ONLY.

    Steps:
      1. assemble the entry-only decision features (each stamped at the decision block_time);
      2. RUN THE LEAK GUARD (`assert_features_leq_decision`) — a forward feature RAISES here;
      3. baseline selection via the FROZEN `evaluate_baseline` (thin first-K proxy);
      4. thin model selection via `thin_model_selected` (documented placeholder).

    `extra_features` flows straight into step 1: injecting a forward observation there trips
    the guard in step 2 — that is the structural leak boundary the leak test proves.
    """
    features = assemble_decision_features(entry, extra_features=extra_features)
    summary = assert_features_leq_decision(features, entry.decision_block_time_ms)
    values = {f.name: f.value for f in features}

    # 3. FROZEN naive-momentum baseline (reused verbatim).
    bp = _thin_buy_pressure_features(entry)
    baseline_signal = evaluate_baseline(bp, baseline_params, mint=entry.mint)

    # 4. THIN model decision (documented placeholder — NOT the trained classifier).
    model_sel, dev_fraction = thin_model_selected(values)

    return DecisionResult(
        model_selected=model_sel,
        baseline_selected=baseline_signal.selected,
        sol_at_risk_lamports=risk_config.per_trade_cap_lamports,
        baseline_momentum_lamports=int(baseline_signal.momentum_score),
        dev_buy_fraction=dev_fraction,
        leak_audit_summary=summary,
    )


# ---------------------------------------------------------------------------
# The OUTCOME step (FORWARD-TIME ONLY) — paper-walk net of the round-trip cost
# ---------------------------------------------------------------------------


def build_round_trip_cost_stack() -> CostStack:
    """The ~6% round-trip cost stack (integer bps), validated by the cost model.

    Lines (sum = 600 bps = ~6%): jito tip 30 + priority 20 + entry slippage 75 + AMM fee 75
    + exit slippage 250 + adverse-selection 150 (UNCALIBRATED floor, C-11).  The EXIT
    slippage (250) is REALIZED inside `walk_position` (SECURE routing); the OTHER 350 bps
    are deducted on top of the walk's realized proceeds so the exit cost is counted ONCE
    (no double count).  `expected_edge_bps` is a coherence placeholder only (the harness
    measures REALIZED net PnL, it does not gate on a fabricated expected edge).
    """
    jito, prio, entry_slip, amm, exit_slip, adverse = 30, 20, 75, 75, 250, 150
    total = jito + prio + entry_slip + amm + exit_slip + adverse  # 600 bps
    stack = CostStack(
        expected_edge_bps=total + 1,  # placeholder > total so the coherence gate passes
        jito_tip_bps=jito,
        priority_fee_bps=prio,
        entry_slippage_bps=entry_slip,
        amm_fee_bps=amm,
        exit_slippage_bps=exit_slip,
        adverse_selection_bps=adverse,
        total_cost_bps=total,
    )
    # Reuse the production cost gate to VALIDATE stack coherence (no negative line, adverse
    # floor met, total reconciles to the sum of parts). Fail closed if incoherent.
    decision = CostAwareEntryGate().evaluate(stack)
    if not decision.passed:
        raise CorpusRecordError(
            f"round-trip cost stack failed coherence validation: {decision.reason}"
        )
    return stack


@dataclass(frozen=True)
class OutcomeResult:
    """The forward-resolved outcome for one launch (integer-lamport money)."""

    net_pnl_lamports: int
    gross_pnl_lamports: int
    round_trip_cost_lamports: int
    realized_multiple: Decimal  # last forward mark / entry price (audit; 0 = rugged/dead)
    n_forward_marks: int
    rugged: bool  # True iff any forward horizon reported NO market (price_sol is None)


def _mint_walk_seed(mint: str) -> int:
    """Stable per-mint seed for the paper-walk sandwich draws (never the salted builtin)."""
    digest = hashlib.sha256(mint.encode("utf-8")).digest()[:8]
    return (_WALK_SEED ^ int.from_bytes(digest, "big")) & 0x7FFFFFFF


def resolve_outcome(
    entry: EntryRecord,
    forward_path: list[ForwardObservation],
    *,
    cost_stack: CostStack,
    sol_at_risk_lamports: int,
    engine: ExitEngine | None = None,
) -> OutcomeResult:
    """Resolve the realized NET PnL from the FORWARD price path (forward-time data ONLY).

    Uses the entry-implied price (entry-time) as the fill and walks the forward marks through
    the SAME production `ExitEngine` (`walk_position`).  A `price_sol is None` horizon is a
    dead/rugged market -> a zero mark (worthless), which trips the hard stop.  The realized
    gross PnL (which already includes the SECURE exit slippage) is then taken NET of the
    remaining round-trip cost (total - exit slippage) so the exit cost is counted once.

    Raises `CorpusRecordError` (CENSORED) when the path is empty or the entry price is
    non-positive — never a fabricated outcome.
    """
    if not forward_path:
        raise CorpusRecordError(f"{entry.mint}: no forward observations (CENSORED)")
    entry_price = entry.entry_price_sol  # Decimal, entry-time (raises if v_tokens <= 0)
    if entry_price <= 0:
        raise CorpusRecordError(f"{entry.mint}: non-positive entry price (CENSORED)")

    # Forward marks as Decimal MULTIPLES of the entry price. None (no market) -> 0 (worthless).
    multiples: list[Decimal] = []
    rugged = False
    for obs in forward_path:
        if obs.price_sol is None:
            multiples.append(Decimal(0))
            rugged = True
        else:
            multiples.append(obs.price_sol / entry_price)

    eng = engine if engine is not None else ExitEngine(config=preset("secure_balanced"))
    walk = walk_position(
        engine=eng,
        mint=entry.mint,
        sol_in_lamports=sol_at_risk_lamports,
        entry_fill_price=Decimal(1),  # marks are already MULTIPLES of entry (R = multiple)
        path=multiples,
        rng=random.Random(_mint_walk_seed(entry.mint)),
        block_time_ms_start=entry.decision_block_time_ms,
    )

    # Round-trip cost NET of the exit slippage already realized inside the walk (no double
    # count). Exact integer lamports from integer bps.
    entry_side_bps = cost_stack.total_cost_bps - cost_stack.exit_slippage_bps
    round_trip_cost_lamports = int(
        Decimal(sol_at_risk_lamports) * Decimal(entry_side_bps) / Decimal(10_000)
    )
    net_pnl = walk.gross_pnl_lamports - round_trip_cost_lamports

    return OutcomeResult(
        net_pnl_lamports=int(net_pnl),
        gross_pnl_lamports=int(walk.gross_pnl_lamports),
        round_trip_cost_lamports=int(round_trip_cost_lamports),
        realized_multiple=(multiples[-1] if multiples else Decimal(0)),
        n_forward_marks=len(multiples),
        rugged=rugged,
    )


# ---------------------------------------------------------------------------
# Corpus reading + record -> EntryRecord (with the injected block_time resolver)
# ---------------------------------------------------------------------------


def read_corpus(path: str | Path) -> Iterator[CorpusRecord]:
    """Yield CorpusRecords from a `labeled_corpus.jsonl` file (one JSON object per line).

    A blank line is skipped; a malformed line is skipped (CENSORED) rather than crashing the
    whole run.  A missing file yields nothing (the caller fails closed on an empty set).
    """
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = obj.get("entry")
            forward = obj.get("forward")
            if not isinstance(entry, dict) or not isinstance(forward, list):
                continue
            yield CorpusRecord(entry=entry, forward=forward)


def _decimal_from(entry: dict, key: str) -> Decimal:
    val = entry.get(key)
    if val is None:
        raise CorpusRecordError(f"entry missing numeric field {key!r} (CENSORED)")
    return Decimal(str(val))


def to_entry_record(record: CorpusRecord, block_time: BlockTime) -> EntryRecord:
    """Build the ENTRY-ONLY view from a corpus record + its RESOLVED on-chain block_time.

    Money fields are parsed exactly via Decimal(str(...)) then quantized to integer lamports
    (data-models §0).  `recv_wall_ms` is retained for monitoring only — it is NEVER used to
    build the decision anchor (that is `block_time`, from getTransaction, T-300a).
    """
    e = record.entry
    if not record.mint:
        raise CorpusRecordError("entry missing mint (CENSORED)")
    sol_amount = _decimal_from(e, "solAmount")
    v_sol = _decimal_from(e, "vSolInBondingCurve")
    v_tokens = _decimal_from(e, "vTokensInBondingCurve")
    market_cap = _decimal_from(e, "marketCapSol")
    initial_buy = _decimal_from(e, "initialBuy")
    return EntryRecord(
        mint=record.mint,
        decision_slot=block_time.slot,
        decision_block_time_ms=block_time.block_time_ms,
        sol_amount_lamports=int((sol_amount * Decimal(LAMPORTS_PER_SOL)).to_integral_value()),
        v_sol_lamports=int((v_sol * Decimal(LAMPORTS_PER_SOL)).to_integral_value()),
        v_tokens_base=v_tokens,
        market_cap_lamports=int((market_cap * Decimal(LAMPORTS_PER_SOL)).to_integral_value()),
        initial_buy_tokens=initial_buy,
        pool=str(e.get("pool", "")),
        recv_wall_ms=int(e.get("recv_wall_ms", 0)),
    )


# ---------------------------------------------------------------------------
# The harness: recorded corpus -> list[TradeOutcome]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessStats:
    """Book-keeping for one harness run (never a win-rate — pure counts)."""

    n_records: int
    n_resolved: int
    n_censored: int
    censor_reasons: tuple[str, ...]


def build_trade_outcomes(
    records: Iterable[CorpusRecord],
    *,
    block_time_resolver: BlockTimeResolver,
    forward_source: ForwardPriceSource | None = None,
    baseline_params: BaselineParams | None = None,
    risk_config: RiskConfig | None = None,
    cost_stack: CostStack | None = None,
) -> tuple[list[TradeOutcome], HarnessStats]:
    """Turn recorded launches into `TradeOutcome` records — the Phase-5 deliverable.

    For each record: resolve the on-chain block_time (injected resolver), split into the
    ENTRY view and the FORWARD view, DECIDE from entry-only data (leak-guarded), RESOLVE the
    outcome from forward-only data, and emit ONE `TradeOutcome`.  A record whose block_time
    cannot be resolved or whose data is unusable is CENSORED (dropped), never fabricated.

    Returns (outcomes, stats).  The DECISION and OUTCOME are computed by two functions that
    never share an argument — the structural leak boundary.
    """
    fsrc = forward_source or EmbeddedForwardPriceSource()
    bparams = baseline_params or load_params()
    rconfig = risk_config or RiskConfig()
    cstack = cost_stack or build_round_trip_cost_stack()
    engine = ExitEngine(config=preset("secure_balanced"))

    outcomes: list[TradeOutcome] = []
    n_records = 0
    censor_reasons: list[str] = []
    for record in records:
        n_records += 1
        try:
            block_time = block_time_resolver.resolve(record.signature)
            entry = to_entry_record(record, block_time)
            forward_path = fsrc.forward_path(record)
            # DECISION — entry-time only (leak-guarded inside decide_entry).
            decision = decide_entry(
                entry, baseline_params=bparams, risk_config=rconfig
            )
            # OUTCOME — forward-time only.
            outcome = resolve_outcome(
                entry,
                forward_path,
                cost_stack=cstack,
                sol_at_risk_lamports=decision.sol_at_risk_lamports,
                engine=engine,
            )
            outcomes.append(
                TradeOutcome(
                    mint=entry.mint,
                    decision_slot=entry.decision_slot,
                    model_selected=decision.model_selected,
                    baseline_selected=decision.baseline_selected,
                    net_pnl_lamports=outcome.net_pnl_lamports,
                    sol_at_risk_lamports=decision.sol_at_risk_lamports,
                )
            )
        except (BlockTimeUnavailable, CorpusRecordError) as exc:
            censor_reasons.append(f"{record.mint or '?'}: {exc}")
            continue

    stats = HarnessStats(
        n_records=n_records,
        n_resolved=len(outcomes),
        n_censored=len(censor_reasons),
        censor_reasons=tuple(censor_reasons),
    )
    return outcomes, stats


def build_from_corpus(
    corpus_path: str | Path,
    *,
    block_time_resolver: BlockTimeResolver,
    forward_source: ForwardPriceSource | None = None,
    baseline_params: BaselineParams | None = None,
    risk_config: RiskConfig | None = None,
    cost_stack: CostStack | None = None,
) -> tuple[list[TradeOutcome], HarnessStats]:
    """Convenience: read a corpus file and build the TradeOutcome list in one call."""
    return build_trade_outcomes(
        read_corpus(corpus_path),
        block_time_resolver=block_time_resolver,
        forward_source=forward_source,
        baseline_params=baseline_params,
        risk_config=risk_config,
        cost_stack=cost_stack,
    )
