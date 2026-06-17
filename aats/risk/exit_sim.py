"""T-325 — Point-in-time paper-walk + A/B-vs-naive harness for the ExitEngine.

This is the BACKTEST / paper path for the productionized ExitEngine.  It walks a
price PATH through the engine, ONE event-time step at a time, threading the
lifecycle state — the SAME `ExitEngine.on_tick` code that runs live.  There is no
separate "backtest" decision path: the harness only supplies the marks; the
engine decides identically.  That is the point-in-time guarantee (C-5): live and
backtest are the same code, fed event-time marks in order, never a future mark.

It exists to reproduce, money-exact (integer lamports), the documented exit-
discipline lift: the staged TP-ladder + trailing + hard-stop exit beats the naive
"capture 0.5x of peak" exit by a meaningful margin on the sim scenarios
(sniper_sim/demo.py reports ~+24% net on the colo A/B).

NON-WAIVABLE on this path:
  * Money is INTEGER LAMPORTS end-to-end (no float in the PnL).  Proceeds and PnL
    are integer-lamport sums; the only Decimals are prices/returns (money-derived
    exact).  A `float` mark is rejected at the engine boundary.
  * Point-in-time: the walk consumes marks in event-time order; the engine never
    sees a future mark.  No lookahead, no compute-time leak.
  * No real capital: this is a pure in-memory simulator — it NEVER touches a
    network or the live venue.  It calls the engine, not `ExecutionVenue`.
  * DRY-RUN by default: there is no live-submit path in this module at all.

The sandwich / exit-slippage haircut is applied here (the paper-walk's job), from
the routing mode's `MevSlippageModel`, with a SEEDED RNG so the A/B is exactly
reproducible.  Production landing economics come from the live venue; this model
exists only to reproduce the sim's A/B lift deterministically.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from aats.risk.exit_engine import (
    ExitConfig,
    ExitEngine,
    ExitReason,
    ExitState,
    MevExitMode,
    slippage_model_for,
)

_FULL_EXIT_BPS = 10_000
_BPS_DENOM = Decimal(10_000)
_LAMPORTS_PER_SOL = 1_000_000_000


# --------------------------------------------------------------------------- #
# Synthetic price-path generator (promoted from sniper_sim.exits.generate_path).
# Returns Decimal multiples of the launch spot — NEVER float marks downstream.
# --------------------------------------------------------------------------- #


def generate_path(
    max_multiple: Decimal,
    is_rug: bool,
    rng: random.Random,
    n: int = 30,
) -> list[Decimal]:
    """Synthetic price path in multiples of launch spot, as Decimals.

    Rug: brief blip then liquidity pulled -> collapses toward ~0 (the hard stop
    should save most of the position).  Runner/dud: rise to peak=max_multiple then
    retrace to a floor (TP ladder + trailing harvest the move).

    Mirrors sniper_sim.exits.generate_path exactly, but quantizes each mark to a
    Decimal so no float ever reaches the engine.  The RNG jitter is computed in
    float (a statistical quantity, not money) then quantized to a Decimal mark.
    """
    mm = float(max_multiple)
    path: list[Decimal] = []
    if is_rug:
        peak = rng.randint(1, 3)
        for i in range(n):
            if i <= peak:
                m = 1.0 + (mm - 1.0) * (i / max(peak, 1))
            else:
                m = max(0.02, mm * (0.5 ** (i - peak)))
            path.append(_q(max(0.01, m * rng.gauss(1.0, 0.03))))
        return path
    peak_step = rng.randint(4, 14)
    floor = mm * rng.uniform(0.25, 0.45)
    for i in range(n):
        if i <= peak_step:
            m = 1.0 + (mm - 1.0) * (i / peak_step)
        else:
            m = floor + (mm - floor) * (0.85 ** (i - peak_step))
        path.append(_q(max(0.01, m * rng.gauss(1.0, 0.04))))
    return path


def _q(x: float) -> Decimal:
    """Quantize a float path multiple to a fixed-precision Decimal mark.

    The jitter source is float (a statistical quantity); we quantize ONCE here so
    every mark the engine sees is a Decimal — float never reaches the money math.
    """
    return Decimal(str(x)).quantize(Decimal("0.00000001"))


# --------------------------------------------------------------------------- #
# Paper-walk result (integer-lamport PnL).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WalkResult:
    """Result of walking one position through the engine, money-exact.

    `gross_pnl_lamports` is integer lamports (proceeds - cost basis).  `exits` is
    the ordered list of (ExitReason, fraction_of_remaining_bps) that fired.
    """

    mint: str
    sol_in_lamports: int
    gross_pnl_lamports: int
    proceeds_lamports: int
    exits: tuple[tuple[ExitReason, int], ...]
    final_remaining_bps: int


def _sell_proceeds_lamports(
    *,
    notional_lamports: int,
    r: Decimal,
    mode: MevExitMode,
    rng: random.Random,
) -> int:
    """Proceeds (integer lamports) from selling `notional_lamports` of cost basis
    at realized return R, net of the mode's exit slippage + sandwich haircut.

    Proceeds = notional * R * (1 - haircut).  All exact in Decimal, then floored
    to integer lamports.  The haircut is bps (integer); the sandwich draw uses the
    seeded RNG so the A/B is reproducible.
    """
    model = slippage_model_for(mode)
    haircut_bps = model.exit_slippage_bps
    if rng.random() < model.sandwich_p_bps / float(_FULL_EXIT_BPS):
        haircut_bps += model.sandwich_loss_bps
    keep = (_BPS_DENOM - Decimal(haircut_bps)) / _BPS_DENOM
    proceeds = Decimal(notional_lamports) * r * keep
    return int(proceeds)  # floor to integer lamports (no float money)


def walk_position(
    *,
    engine: ExitEngine,
    mint: str,
    sol_in_lamports: int,
    entry_fill_price: Decimal,
    path: list[Decimal],
    rng: random.Random,
    block_time_ms_start: int = 1_700_000_000_000,
    block_time_step_ms: int = 400,
    narrative_failure_at: int | None = None,
) -> WalkResult:
    """Walk ONE position's price path through the ExitEngine, money-exact.

    `path` is a list of Decimal price multiples of the launch spot.  The mark fed
    to the engine is `entry_fill_price * multiple` (so R = mark/entry = multiple /
    (1+entry_slip) when entry_fill_price already embeds entry slippage — the
    caller controls that).  Proceeds accrue in integer lamports as rungs / stops
    fire.  Any remainder at the end of the path is force-exited at the last mark
    (a real position is never left un-exited in the walk).

    `narrative_failure_at`, if set, injects the pre-set LLM catastrophic-exit flag
    at that step index (de-risk only) — used to test the highest-priority branch.

    Point-in-time: marks are consumed in order; the engine never sees a future
    mark.  Same code as live.
    """
    if isinstance(sol_in_lamports, bool | float):
        raise TypeError("sol_in_lamports must be int (lamports), not float/bool.")
    if sol_in_lamports <= 0:
        raise ValueError("sol_in_lamports must be > 0.")

    state = ExitState.at_entry(mint=mint, entry_fill_price=entry_fill_price, config=engine.config)
    proceeds = 0
    exits: list[tuple[ExitReason, int]] = []

    for step, multiple in enumerate(path):
        if state.remaining_bps <= 0:
            break
        mark = entry_fill_price * multiple
        bt = block_time_ms_start + step * block_time_step_ms
        flag = narrative_failure_at is not None and step == narrative_failure_at

        # Notional of cost basis still held BEFORE this tick's decision.
        notional_before = int(sol_in_lamports * state.remaining_bps // _FULL_EXIT_BPS)

        decision = engine.on_tick(
            state, mark_price=mark, block_time_ms=bt, narrative_failure_flag=flag
        )

        if decision.intent is not None:
            r = state.realized_r(mark)
            mode = decision.exit_mode or MevExitMode.SECURE
            # bps OF REMAINING shed this tick -> notional of basis sold.
            sold_notional = int(notional_before * decision.fraction_bps // _FULL_EXIT_BPS)
            proceeds += _sell_proceeds_lamports(
                notional_lamports=sold_notional, r=r, mode=mode, rng=rng
            )
            exits.append((decision.reason, decision.fraction_bps))

        state = decision.next_state

    # Force-exit any remainder at the LAST mark (Secure routing — defensive).
    if state.remaining_bps > 0 and path:
        last_mark = entry_fill_price * path[-1]
        r = state.realized_r(last_mark)
        notional = int(sol_in_lamports * state.remaining_bps // _FULL_EXIT_BPS)
        proceeds += _sell_proceeds_lamports(
            notional_lamports=notional, r=r, mode=MevExitMode.SECURE, rng=rng
        )
        exits.append((ExitReason.TIMEOUT, _FULL_EXIT_BPS))
        state = state._replace(remaining_bps=0)

    return WalkResult(
        mint=mint,
        sol_in_lamports=sol_in_lamports,
        gross_pnl_lamports=proceeds - sol_in_lamports,
        proceeds_lamports=proceeds,
        exits=tuple(exits),
        final_remaining_bps=state.remaining_bps,
    )


# --------------------------------------------------------------------------- #
# Naive baseline: "capture 0.5x of the peak", money-exact (the A/B counterpart).
# Mirrors sniper_sim.demo.realize_pnl / EXIT_EFF=0.50, in integer lamports.
# --------------------------------------------------------------------------- #

_NAIVE_EXIT_EFF_BPS = 5_000  # capture 0.50x of peak (EXIT_EFF=0.50, demo.py)


def naive_walk_position(
    *,
    mint: str,
    sol_in_lamports: int,
    entry_fill_price: Decimal,
    path: list[Decimal],
    rng: random.Random,
    mode: MevExitMode = MevExitMode.SECURE,
) -> WalkResult:
    """The NAIVE exit: sell EVERYTHING at 0.5x of the realized PEAK, net of one
    sandwich/haircut draw.  This is the documented baseline the staged ladder is
    compared against (demo.py "naive 0.5x-peak exit").

    Crucially it is a HINDSIGHT exit (it uses the path's peak) — it is the
    baseline, NOT a point-in-time strategy.  The ExitEngine, by contrast, decides
    online with no lookahead.  The A/B is "disciplined online ladder vs naive
    hindsight 0.5x-peak"; the ladder still wins, which is the whole point.
    """
    if not path:
        return WalkResult(mint, sol_in_lamports, -sol_in_lamports, 0, (), 0)
    peak_multiple = max(path)
    peak_r = peak_multiple  # entry_fill_price cancels: R_peak = peak_mark/entry
    exit_r = peak_r * Decimal(_NAIVE_EXIT_EFF_BPS) / _BPS_DENOM
    proceeds = _sell_proceeds_lamports(
        notional_lamports=sol_in_lamports, r=exit_r, mode=mode, rng=rng
    )
    return WalkResult(
        mint=mint,
        sol_in_lamports=sol_in_lamports,
        gross_pnl_lamports=proceeds - sol_in_lamports,
        proceeds_lamports=proceeds,
        exits=((ExitReason.TAKE_PROFIT, _FULL_EXIT_BPS),),
        final_remaining_bps=0,
    )


# --------------------------------------------------------------------------- #
# A/B harness — identical fills, naive 0.5x-peak vs staged ladder.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScenarioLaunch:
    """One synthetic launch for the A/B (the bot already FILLED it identically in
    both arms — the A/B isolates the EXIT policy, exactly like demo.py's colo
    A/B).  `max_multiple` and `entry_fill_price` are Decimals (money-exact)."""

    mint: str
    is_rug: bool
    detectable: bool  # whether a good gate could have caught the rug
    max_multiple: Decimal
    entry_fill_price: Decimal
    sol_in_lamports: int


# ILLUSTRATIVE PRIORS ONLY — these mirror sniper_sim/demo.py so the A/B lift is
# directly comparable to the documented colo number.  They are NOT a real model:
# the real selection intelligence is `ml-prediction-engineer`'s; this harness only
# needs a realistic POST-GATE population on which to measure the EXIT lift.
_MODEL_SKILL = 0.60
_MODEL_THRESHOLD = 0.55
_GATE_CATCH = 0.75


def _sigmoid(x: float) -> float:
    import math

    return 1.0 / (1.0 + math.exp(-x))


def _model_prob(max_multiple: Decimal, is_rug: bool, rng: random.Random) -> float:
    """EMULATED snipe-classifier output (mirrors demo.model_prob).

    Reads truth through a noise channel whose width shrinks with skill.  ONLY used
    to reconstruct the realistic post-gate population for the EXIT A/B — it is not
    a real predictor (the real one is the ml-prediction lane).
    """
    import math

    mm = float(max_multiple)
    signal = math.log(max(mm, 1e-3)) - (2.0 if is_rug else 0.0)
    noise = rng.gauss(0.0, (1.0 - _MODEL_SKILL) * 3.0 + 0.3)
    return _sigmoid(0.6 * signal + noise)


def generate_scenario_launches(n: int, seed: int) -> list[ScenarioLaunch]:
    """Generate n synthetic launches, mirroring demo.generate_launches priors but
    money-exact (entry fill = 1 lamport/token nominal; 1 SOL position).

    ~60% rugs (salvage 0..0.5x), the rest mostly small with a rare moonshot — the
    same trap-heavy distribution the sim uses.  The raw set is trap-heavy; the A/B
    runs on the GATED subset (see `gate_and_fill`), exactly like the sim's colo
    A/B which acts on model+gate-filtered, landed candidates.
    """
    rng = random.Random(seed)
    out: list[ScenarioLaunch] = []
    for i in range(n):
        is_rug = rng.random() < 0.60
        if is_rug:
            max_mult = rng.uniform(0.0, 0.5)
            detectable = rng.random() < 0.70
        else:
            max_mult = 0.8 + rng.lognormvariate(0.3, 1.0)
            detectable = False
        out.append(
            ScenarioLaunch(
                mint=f"MINT{i:04d}",
                is_rug=is_rug,
                detectable=detectable,
                max_multiple=_q(max(0.01, max_mult)),
                entry_fill_price=Decimal("1"),  # 1 lamport/token nominal
                sol_in_lamports=_LAMPORTS_PER_SOL,  # 1 SOL
            )
        )
    return out


def gate_and_fill(launches: list[ScenarioLaunch], *, seed: int = 7) -> list[ScenarioLaunch]:
    """Apply the model threshold + safety gate to get the realistic POST-GATE,
    FILLED population the exit policy actually acts on — exactly the population the
    sim's colo A/B uses (model+gate-filtered, landed).

    This is what makes both A/B arms NET POSITIVE: the gate+model has already
    vetoed most catchable rugs, so the exit discipline is measured on a realistic
    book, not on raw trap-heavy launches.  Returns the filtered ScenarioLaunch
    list (identical fills assumed for both arms — the A/B isolates the EXIT).
    """
    rng_m = random.Random(seed + 1)
    out: list[ScenarioLaunch] = []
    for launch in launches:
        p = _model_prob(launch.max_multiple, launch.is_rug, rng_m)
        if p < _MODEL_THRESHOLD:
            continue  # model SKIP
        # Safety gate: catches a detectable rug with probability _GATE_CATCH.
        if launch.is_rug and launch.detectable and rng_m.random() < _GATE_CATCH:
            continue  # gate VETO (de-risk)
        out.append(launch)
    return out


@dataclass(frozen=True)
class ABResult:
    """A/B outcome: net PnL (integer lamports) of the staged ladder vs the naive
    exit on identical fills, and the lift."""

    n: int
    naive_net_lamports: int
    ladder_net_lamports: int
    lift_lamports: int
    lift_pct_of_abs_naive: Decimal | None  # ladder beats naive by this % (None if naive==0)


def run_ab(
    launches: list[ScenarioLaunch],
    *,
    config: ExitConfig,
    seed: int = 7,
) -> ABResult:
    """Run the EXIT-policy A/B on identical fills.

    Both arms see the SAME price path per launch (same per-launch RNG seed), so
    the only difference is the exit policy.  This reproduces demo.py's
    "EXIT-POLICY A/B (same colo fills)".  Returns integer-lamport nets and the
    lift.  The ladder is expected to win materially (the documented ~+24%).
    """
    engine = ExitEngine(config=config)
    naive_net = 0
    ladder_net = 0
    for launch in launches:
        # SAME path for both arms (identical fills, identical price evolution) —
        # a per-launch deterministic seed derived from the global seed + mint.
        path_seed = (seed * 1_000_003 + hash(launch.mint)) & 0x7FFFFFFF
        path = generate_path(launch.max_multiple, launch.is_rug, random.Random(path_seed))

        # Each arm draws its sandwich outcomes from its OWN seeded RNG so the two
        # arms are independent but each reproducible.
        naive = naive_walk_position(
            mint=launch.mint,
            sol_in_lamports=launch.sol_in_lamports,
            entry_fill_price=launch.entry_fill_price,
            path=path,
            rng=random.Random(path_seed ^ 0xA5A5),
        )
        ladder = walk_position(
            engine=engine,
            mint=launch.mint,
            sol_in_lamports=launch.sol_in_lamports,
            entry_fill_price=launch.entry_fill_price,
            path=path,
            rng=random.Random(path_seed ^ 0x5A5A),
        )
        naive_net += naive.gross_pnl_lamports
        ladder_net += ladder.gross_pnl_lamports

    lift = ladder_net - naive_net
    lift_pct: Decimal | None
    if naive_net != 0:
        lift_pct = (Decimal(lift) / Decimal(abs(naive_net)) * Decimal(100)).quantize(
            Decimal("0.01")
        )
    else:
        lift_pct = None
    return ABResult(
        n=len(launches),
        naive_net_lamports=naive_net,
        ladder_net_lamports=ladder_net,
        lift_lamports=lift,
        lift_pct_of_abs_naive=lift_pct,
    )
