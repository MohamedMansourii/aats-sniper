"""Bootstrap SYNTHETIC labeled launch dataset (T-310) — NOT REAL DATA.

================================ READ THIS FIRST ================================
THIS IS BOOTSTRAP DATA, NOT RECORDED CHAIN DATA (brief 7.2).  No model trained on
this dataset has earned ANY capital license.  The sole production-acceptance metric is
net-of-cost PnL + model-vs-baseline on RECORDED data (the clean-room harness, T-400/401)
— never on this synthetic set.  Every artifact derived from this module is stamped
`IS_BOOTSTRAP_NOT_REAL = True` and the model card records it.  Real capital stays
DISABLED behind DRY-RUN regardless of any number this dataset produces.
================================================================================

WHY IT EXISTS
=============
Recorded launch data does not exist yet (T-400/401 not built).  To build and TEST the
training -> calibrate -> ONNX-export -> inference pipeline END TO END now, we need a
labeled dataset with the *shape* of the real one:
  - point-in-time `FeatureFrame` rows (the EXACT production contract), and
  - `LaunchOutcome` label rows in a SEPARATE event-time-keyed dataset (labels/),
joined ONLY by event_time — the same leak-free construction the harness enforces.

LEAK-FREE BY THE SAME CONSTRUCTION AS PRODUCTION
================================================
  - Features and labels are returned as TWO disjoint datasets.  The label is NEVER a
    field on a FeatureFrame (the contract's extra="forbid" + _no_label_fields guard
    would reject it anyway).
  - The label is a function of a HIDDEN latent quality + structural risk factors +
    noise.  The latent quality is partially OBSERVABLE through the features (so a real
    model can legitimately learn signal), but the label itself is never copied into any
    feature.  There is no `realized_mult` or `survived` column anywhere in the frames.
  - `resolution_event_time = event_time + H` is stamped and strictly later, exactly as
    the production label contract requires.
  - Event-time ordering is monotone across the generated corpus so a walk-forward split
    is meaningful (train = earlier slots, test = later slots).

HONEST DIFFICULTY (so the test is not a tautology)
==================================================
The label has substantial irreducible noise AND adversarial structure:
  - High `synchronicity` / coordinated-shill / low-account-age launches are MORE likely
    to rug (manufactured hype is a CONTRARIAN risk signal — BUILD-DIRECTIVE).  But those
    social fields live in MCSScore, not FeatureFrame; here we encode the same idea
    through on-chain proxies: high top-10 concentration, high sell tax, and being far
    behind smart money correlate with a worse outcome.
  - The naive-momentum baseline (positive first-K buy pressure) captures PART of the
    signal but is fooled by hype-pumped rugs (high buy pressure that still rugs), so a
    well-specified model can beat it out-of-sample — but only modestly, and only if it
    reads the risk features.  An overfit or leaking model is caught by the no-leak and
    calibration tests, not rewarded.

MONEY DISCIPLINE
================
All lamport quantities are integers.  first_k_buy_pressure is Decimal.  No float money.
The generator's randomness is statistical (probabilities), never money.

DETERMINISM
===========
Fully seeded (numpy Generator(PCG64) with a fixed seed) — the same seed reproduces the
same corpus byte-for-byte, so the model card metrics are reproducible (self-check 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from aats.contracts.events import EventTime
from aats.contracts.features import FeatureFrame, FeatureProvenance, FeatureSourceWindow
from aats.features.buy_pressure import FEATURE_SCHEMA_VERSION

# Loud, unmissable marker.  Imported and asserted by the model card + tests.
IS_BOOTSTRAP_NOT_REAL = True

# Generative constants (synthetic units; lamports are integers).
LAMPORTS_PER_SOL = 1_000_000_000
_BASE_SLOT = 320_000_000
_BASE_BLOCK_TIME_MS = 1_718_000_000_000
_SLOT_MS = 400  # ~Solana slot time, for stamping block_time monotone with slot
DEFAULT_FIRST_K_SLOTS = 10
DEFAULT_LABEL_HORIZON_H_SLOTS = 1_500  # ~10 min at 400ms/slot (the survivor horizon)

# The synthetic label is binary for the snipe classifier: 1 == "SURVIVED-and-pumped"
# (reached the target multiple AND remained sellable), 0 otherwise (FADED/RUGGED).
# This mirrors the leak-free label spec: P(coin reaches Xx within T AND remains sellable).


@dataclass(frozen=True)
class SyntheticRow:
    """One synthetic launch: a production FeatureFrame + its post-hoc binary label.

    The frame is the EXACT production contract.  The label is carried SEPARATELY here
    (never as a frame field) and is written to a disjoint labels dataset by the caller.
    `event_time` is the join key for both — features and labels join ONLY on this.
    """

    frame: FeatureFrame
    # Binary outcome label (1 = survived-and-pumped, 0 = faded/rugged).  Lives in
    # labels/, joined by event_time only — NEVER a field on `frame`.
    label: int
    # Stamped resolution time = event_time + H (proves horizon, as production requires).
    resolution_event_time: EventTime
    label_horizon_h_slots: int


def _make_event_time(slot: int) -> EventTime:
    return EventTime(
        slot=slot,
        block_time_ms=_BASE_BLOCK_TIME_MS + (slot - _BASE_SLOT) * _SLOT_MS,
        # wall_clock is monitoring-only; set it >= block_time but never used as a join key.
        wall_clock_ms=_BASE_BLOCK_TIME_MS + (slot - _BASE_SLOT) * _SLOT_MS + 5,
    )


def _provenance_within_cutoff(event_slot: int, first_k_slots: int) -> FeatureProvenance:
    """Build a provenance manifest whose every window is <= event_time + K (clean)."""
    cutoff = event_slot + first_k_slots
    # All synthetic features read from launch_events within the K window (max_source_slot
    # = cutoff exactly — the latest legal slot).  No window touches labels/.
    feat_names = (
        "first_k_buy_pressure",
        "first_k_volume_lamports",
        "first_k_buy_count",
        "first_k_sell_count",
        "first_k_unique_buyers",
        "lp_depth_lamports",
        "holder_count",
        "holder_concentration_top10_bps",
        "sniper_cluster_score",
        "sell_tax_bps",
        "smart_wallets_in",
        "tip_floor_at_decision_lamports",
    )
    windows = [
        FeatureSourceWindow(
            feature_name=name,
            max_source_slot=cutoff,  # the latest LEGAL slot — guard passes (<= cutoff)
            lineage_dataset="launch_events",
        )
        for name in feat_names
    ]
    return FeatureProvenance(windows=windows, builder_version="synthetic-bootstrap-1.0.0")


def generate_synthetic_corpus(
    n: int = 4000,
    *,
    seed: int = 20260616,
    first_k_slots: int = DEFAULT_FIRST_K_SLOTS,
    label_horizon_h_slots: int = DEFAULT_LABEL_HORIZON_H_SLOTS,
) -> list[SyntheticRow]:
    """Generate `n` synthetic launches, event-time ordered (monotone slot).

    Returns a list of SyntheticRow (frame + separate label).  The frames are valid
    production FeatureFrame instances; the labels live separately and join by event_time.

    The latent generative model (NOT observable to the trained model except through the
    features it legitimately reads):

        quality ~ N(0, 1)                              # hidden launch quality
        observed positively through:  buy pressure, unique-buyer velocity, lp_depth
        observed negatively through:  top10 concentration, sell tax, behind-smart-money

        rug_pressure = w1*concentration + w2*sell_tax + w3*behind_smart_money + noise
        survive_logit = a*quality - b*rug_pressure + eps    # eps = irreducible noise
        label = 1 if sigmoid(survive_logit) draws a 1

    The baseline (positive first-K buy pressure) sees `quality`'s positive proxy but is
    BLIND to rug_pressure, so a model that reads the risk features can beat it OOS.
    """
    rng = np.random.default_rng(seed)
    rows: list[SyntheticRow] = []

    slot = _BASE_SLOT
    for _ in range(n):
        slot += int(rng.integers(20, 200))  # monotone, irregular spacing (event-time order)
        event_time = _make_event_time(slot)

        # --- hidden latent quality (drives the outcome, observed only via features) ---
        quality = float(rng.normal(0.0, 1.0))

        # --- structural risk factors (adversarial / contrarian — push outcome DOWN) ---
        # Higher concentration, higher sell tax, more "behind smart money" => more rug.
        concentration_bps = int(np.clip(rng.normal(4000, 2000), 0, 10000))
        sell_tax_bps = int(np.clip(rng.gamma(1.5, 300), 0, 9900))
        smart_wallets_in = int(rng.poisson(0.6))  # >0 means smarter money already in (behind)
        # entry lag: only meaningful if smart wallets are in; else None
        if smart_wallets_in > 0:
            smart_wallet_entry_lag_slots: int | None = int(rng.integers(1, 8))
        else:
            smart_wallet_entry_lag_slots = None

        # --- first-K microstructure (positively related to quality + noise) ---
        base_vol_sol = max(0.05, rng.gamma(2.0, 1.5) * (1.0 + 0.4 * quality))
        first_k_volume_lamports = int(base_vol_sol * LAMPORTS_PER_SOL)
        # net buy pressure: positive in good launches, can be negative in dumps
        net_pressure_sol = rng.normal(0.6 * quality, 1.2)
        first_k_buy_pressure = Decimal(str(round(net_pressure_sol * LAMPORTS_PER_SOL)))
        buy_count = int(np.clip(rng.poisson(max(0.1, 8 + 4 * quality)), 0, None))
        sell_count = int(np.clip(rng.poisson(max(0.1, 4 + 2 * max(0.0, -quality))), 0, None))
        unique_buyers = int(np.clip(rng.poisson(max(0.1, 5 + 3 * quality)), 0, buy_count + 1))

        lp_depth_lamports = int(max(1, rng.gamma(2.0, 2.0) * max(0.1, 1.0 + 0.3 * quality)) * LAMPORTS_PER_SOL)
        holder_count = int(np.clip(rng.poisson(max(0.5, 30 + 15 * quality)), 1, None))
        sniper_cluster_score = float(np.clip(rng.beta(2, 5) + 0.1 * smart_wallets_in, 0.0, 1.0))

        # --- TA: present only for ~60% of launches (enough bar history) ---
        has_ta = rng.random() < 0.6
        rsi = float(np.clip(rng.normal(55 + 5 * quality, 15), 0, 100)) if has_ta else None
        macd = float(rng.normal(0.0 + 0.2 * quality, 0.5)) if has_ta else None
        bb_width = float(np.clip(rng.gamma(2.0, 0.1), 0, 5)) if has_ta else None

        # --- tip contention (purely contextual; one-hot in the model) ---
        tip_bucket = str(rng.choice(["low", "medium", "high"], p=[0.5, 0.35, 0.15]))
        tip_floor_lamports = int(
            {"low": 50_000, "medium": 200_000, "high": 800_000}[tip_bucket]
            * (1.0 + 0.5 * rng.random())
        )

        # --- the outcome (leak-free: a function of latent quality + risk, NOT copied to features) ---
        rug_pressure = (
            1.4 * (concentration_bps / 10000.0)
            + 1.1 * (min(sell_tax_bps, 5000) / 5000.0)
            + 0.8 * (smart_wallets_in > 0)
        )
        eps = float(rng.normal(0.0, 1.0))  # irreducible noise — keeps the task honest
        survive_logit = 1.3 * quality - 1.6 * rug_pressure + 0.7 * eps - 0.3
        p_survive = 1.0 / (1.0 + np.exp(-survive_logit))
        label = int(rng.random() < p_survive)

        resolution_slot = slot + label_horizon_h_slots
        resolution_event_time = _make_event_time(resolution_slot)

        frame = FeatureFrame(
            mint=f"SynthMint{slot:020d}",
            event_time=event_time,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            data_staleness_ms=int(rng.integers(0, 500)),
            first_k_slots=first_k_slots,
            first_k_buy_pressure=first_k_buy_pressure,
            first_k_volume_lamports=first_k_volume_lamports,
            first_k_buy_count=buy_count,
            first_k_sell_count=sell_count,
            first_k_unique_buyers=unique_buyers,
            lp_depth_lamports=lp_depth_lamports,
            holder_count=holder_count,
            holder_concentration_top10_bps=concentration_bps,
            sniper_cluster_score=sniper_cluster_score,
            sell_tax_bps=sell_tax_bps,
            sell_reserve_trajectory=[lp_depth_lamports, max(0, lp_depth_lamports - int(rng.integers(0, lp_depth_lamports + 1)))],
            rsi=rsi,
            macd=macd,
            bb_width=bb_width,
            smart_wallets_in=smart_wallets_in,
            smart_wallet_entry_lag_slots=smart_wallet_entry_lag_slots,
            tip_floor_at_decision_lamports=tip_floor_lamports,
            tip_contention_bucket=tip_bucket,  # type: ignore[arg-type]
            normalization_window="as_of_event_time",
            provenance=_provenance_within_cutoff(slot, first_k_slots),
        )

        rows.append(
            SyntheticRow(
                frame=frame,
                label=label,
                resolution_event_time=resolution_event_time,
                label_horizon_h_slots=label_horizon_h_slots,
            )
        )

    return rows
