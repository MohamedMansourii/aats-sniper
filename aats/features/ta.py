"""Survivor Technical Analysis (TA) features: RSI(14), MACD(12,26,9), BB(20,2σ) (T-304).

These features are computed on resampled OHLCV bars using pandas-ta (pure Python,
pip-installable, no libta-lib C dependency) and are ONLY valid for tokens that have
survived long enough to accumulate meaningful history.

CRITICAL MIN-HISTORY GUARD
---------------------------
A freshly-minted token has existed for seconds.  RSI requires 14 bars, MACD
requires 26 bars, BB requires 20 bars.  A 90-second-old token on 15-second bars
has 6 bars — not enough for ANY of these indicators.

DO NOT treat RSI/MACD/BB on a 40-second-old token as signal.  These values are
NOT emitted for tokens below their lookback threshold:

    rsi: float | None      ← None if fewer than RSI_LOOKBACK bars
    macd: float | None     ← None if fewer than MACD_SLOW + MACD_SIGNAL bars
    bb_width: float | None ← None if fewer than BB_LOOKBACK bars
    macd_signal: float | None
    macd_hist: float | None
    bb_pct_b: float | None

The validity flags are explicit:
    rsi_valid: bool        ← False = min-history guard fired; do not use rsi
    macd_valid: bool
    bb_valid: bool

LOOP PLACEMENT: SLOW loop only.
    These are NOT in the SNIPE loop.  They are computed in the SLOW loop
    pipeline after the token has accumulated sufficient bar history.
    Microstructure features (microstructure.py) serve the SNIPE loop.

POINT-IN-TIME CORRECTNESS
--------------------------
OHLCV bars are assembled from events with slot <= event_time.slot.
No centered rolling windows.  No forward fill from future bars.
The pandas-ta call is applied to a bar frame that contains ONLY bars with
bar_close_slot <= event_time.slot.

PANDAS-TA vs TA-LIB
--------------------
We use pandas-ta (pure Python, no C build step).  If a SLOW-loop profiled
hotspot proves pandas-ta too slow, propose TA-Lib via the Orchestrator as a
blueprint delta (ADR).  Never add TA-Lib silently.

MONEY RULE
----------
OHLCV amounts: lamports (int).  TA output values are statistical ratios/prices,
not money, so float is correct for: rsi, macd, macd_signal, macd_hist, bb_upper,
bb_middle, bb_lower, bb_pct_b, bb_bandwidth.  The money rule does not apply to
statistical feature quantities.

INCREMENTAL / STREAMING PARITY
-------------------------------
The offline (batch) path and the online path use the same bar assembly function.
There is no separate streaming TA — the SLOW loop always has a bar history
(it is not microsecond-latency-critical like the SNIPE loop).

PRICE-PATH TENSOR FOR THE CHART-PATH / REGIME MODEL (M2-CP-01)
-----------------------------------------------------------------
This module also builds `PricePathTensor` — a normalized, fixed-length,
point-in-time price-path sequence (log-return / drawdown-from-peak /
holder-delta / volume-delta channels + a presence/CENSORED mask) that is the
honest INPUT for the (separately gated, M2-CP-02/04) chart-path/regime model.
It reuses the SAME proven OHLCVBar assembly above — no new price source.
See the "Price-path tensor" section below for the full contract.  This is a
NEW artifact, not a FeatureFrame field; wiring it into a typed contract is
CP-04's job (an ADR, solana-systems-architect) — this module never touches
aats.contracts.features.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TA lookback constants (must match pandas-ta defaults)
# ---------------------------------------------------------------------------

RSI_LOOKBACK: int = 14       # RSI period
MACD_FAST: int = 12          # MACD fast EMA
MACD_SLOW: int = 26          # MACD slow EMA
MACD_SIGNAL: int = 9         # MACD signal line EMA
BB_LOOKBACK: int = 20        # Bollinger Band SMA period
BB_STD: float = 2.0          # Bollinger Band standard deviation multiplier

# Minimum number of OHLCV bars required for each indicator.
# Any fewer bars → emit None with valid=False.  Never fabricate garbage values.
RSI_MIN_BARS: int = RSI_LOOKBACK + 1         # 15 bars
MACD_MIN_BARS: int = MACD_SLOW + MACD_SIGNAL  # 35 bars
BB_MIN_BARS: int = BB_LOOKBACK + 1           # 21 bars

FEATURE_SCHEMA_VERSION = "1.0.0"

_LINEAGE_DATASET: Literal["launch_events"] = "launch_events"


# ---------------------------------------------------------------------------
# OHLCV Bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OHLCVBar:
    """A single OHLCV bar assembled from events.

    All price fields are lamports (int, not float — money rule).
    Volume is in lamports (int).
    The bar is indexed by its CLOSE slot (the last slot contributing to it).
    """

    close_slot: int        # slot of the last event contributing to this bar
    open_lamports: int     # lamports, int
    high_lamports: int     # lamports, int
    low_lamports: int      # lamports, int
    close_lamports: int    # lamports, int
    volume_lamports: int   # total traded volume in lamports, int

    # holder_count: the LAST KNOWN holder count as of this bar's close (carried
    # forward from an earlier bar if not updated within this one — forward-filling
    # a PAST known value into a later bar is point-in-time legal; only forward-
    # filling from a FUTURE value is not).  None = no holder_count ever observed
    # yet.  Optional so existing bar-assembly call sites (price/volume only) are
    # unaffected (M2-CP-01: price-path tensor holder-delta channel).
    holder_count: int | None = None


# ---------------------------------------------------------------------------
# Typed TA result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TAFeatures:
    """TA indicator values for a token with sufficient bar history.

    None fields indicate the min-history guard fired — do not use as signal.
    Valid flags make the guard state explicit to consumers.

    rsi : float | None
        Relative Strength Index, [0, 100].  High RSI (e.g. >70) = overbought.
        None if fewer than RSI_MIN_BARS available.

    macd : float | None
        MACD line = EMA(close, MACD_FAST) - EMA(close, MACD_SLOW).
        In lamports (price units), float.
        None if fewer than MACD_MIN_BARS available.

    macd_signal : float | None
        Signal line = EMA(macd, MACD_SIGNAL).  In lamports, float.
        None if fewer than MACD_MIN_BARS available.

    macd_hist : float | None
        MACD histogram = macd - macd_signal.  In lamports, float.
        None if fewer than MACD_MIN_BARS available.

    bb_upper : float | None
        Bollinger Band upper (SMA + 2σ).  In lamports, float.
        None if fewer than BB_MIN_BARS available.

    bb_middle : float | None
        Bollinger Band middle (SMA(20)).  In lamports, float.
        None if fewer than BB_MIN_BARS available.

    bb_lower : float | None
        Bollinger Band lower (SMA - 2σ).  In lamports, float.
        None if fewer than BB_MIN_BARS available.

    bb_pct_b : float | None
        %B = (close - bb_lower) / (bb_upper - bb_lower).
        [0, 1] range (can exceed for extreme moves).  float.
        None if fewer than BB_MIN_BARS available.

    bb_bandwidth : float | None
        Bandwidth = (bb_upper - bb_lower) / bb_middle.  Volatility regime signal.
        None if fewer than BB_MIN_BARS available.

    rsi_valid, macd_valid, bb_valid : bool
        True if the corresponding indicator has sufficient history.
        False = min-history guard fired; the None value should NOT be used as signal.

    bar_count : int
        Number of OHLCV bars in the history at this event_time.
    """

    rsi: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    bb_pct_b: float | None
    bb_bandwidth: float | None

    rsi_valid: bool
    macd_valid: bool
    bb_valid: bool

    bar_count: int
    max_bar_slot: int  # highest slot in the bar history (for provenance)

    @property
    def bb_width(self) -> float | None:
        """Alias for bb_bandwidth — the FeatureFrame field name is bb_width."""
        return self.bb_bandwidth


# ---------------------------------------------------------------------------
# Bar assembler — builds OHLCV bars from event streams
# ---------------------------------------------------------------------------


@dataclass
class OHLCVBarAssembler:
    """Assembles OHLCV bars from a sequence of LaunchEvent observations.

    Bars are defined by slot ranges.  Each event contributes to the bar
    that covers its slot.  The bar is closed (appended to history) when an
    event in a later slot range arrives.

    Point-in-time guarantee:
        Only events with slot <= cutoff are accepted.  The bar history
        at any point in time contains ONLY bars with close_slot <= event_time.slot.

    This assembler is used by the SLOW loop to maintain a bar history per mint.
    """

    slots_per_bar: int = 10    # how many slots constitute one bar
    _bars: list[OHLCVBar] = field(default_factory=list)
    # Current bar state
    _current_bar_start_slot: int | None = field(default=None)
    _current_open: int = field(default=0)
    _current_high: int = field(default=0)
    _current_low: int = field(default=int(2**62))  # large initial value
    _current_close: int = field(default=0)
    _current_volume: int = field(default=0)
    # Last known holder_count (persists ACROSS bars — a snapshot value, not a
    # per-bar accumulation like volume; carrying a PAST known value forward into
    # a bar where it was not updated is point-in-time legal).  None = never
    # observed.  M2-CP-01: feeds the price-path tensor's holder-delta channel.
    _last_known_holder_count: int | None = field(default=None)

    def observe(
        self,
        slot: int,
        price_lamports: int,
        volume_lamports: int,
        holder_count: int | None = None,
    ) -> None:
        """Add a price/volume observation at a given slot.

        price_lamports : int (lamports — not float)
        volume_lamports : int (lamports — not float)
        slot : int
        holder_count : int | None
            Optional holder-count snapshot at this slot (base-unit-agnostic
            wallet count).  None = not observed at this call; the assembler
            keeps the LAST known value and stamps it onto every subsequent bar
            until a newer value arrives.  Never fabricated, never averaged.

        All int, no float.
        """
        if not isinstance(price_lamports, int):
            raise TypeError(f"price_lamports must be int, got {type(price_lamports)}")
        if not isinstance(volume_lamports, int):
            raise TypeError(f"volume_lamports must be int, got {type(volume_lamports)}")
        if not isinstance(slot, int):
            raise TypeError(f"slot must be int, got {type(slot)}")
        if holder_count is not None and not isinstance(holder_count, int):
            raise TypeError(f"holder_count must be int or None, got {type(holder_count)}")

        # NOTE (point-in-time ordering — do NOT hoist this above the flush below):
        # _last_known_holder_count must be updated to a NEW value only AFTER any
        # flush of the bar it does NOT belong to.  If a bar boundary is crossed on
        # this call, _flush_current_bar() below must stamp the OLD (about-to-close)
        # bar with the holder_count that was known through that OLD bar's own
        # observations — not with the new value this call is reporting for the
        # slot that is starting the NEXT bar.  Updating this field too early would
        # leak a "future" (from the old bar's perspective) holder_count backward
        # onto the bar being flushed.

        bar_start = (slot // self.slots_per_bar) * self.slots_per_bar

        if self._current_bar_start_slot is None:
            # First observation
            self._current_bar_start_slot = bar_start
            self._current_open = price_lamports
            self._current_high = price_lamports
            self._current_low = price_lamports
            self._current_close = price_lamports
            self._current_volume = volume_lamports
            if holder_count is not None:
                self._last_known_holder_count = holder_count
            return

        if bar_start == self._current_bar_start_slot:
            # Same bar — holder_count (if any) applies to this still-open bar.
            if price_lamports > self._current_high:
                self._current_high = price_lamports
            if price_lamports < self._current_low:
                self._current_low = price_lamports
            self._current_close = price_lamports
            self._current_volume += volume_lamports
            if holder_count is not None:
                self._last_known_holder_count = holder_count
        else:
            # New bar: flush the OLD bar FIRST (using the holder_count already
            # known as of the old bar), THEN apply this call's holder_count (if
            # any) to the NEW bar that is starting.
            self._flush_current_bar()
            if holder_count is not None:
                self._last_known_holder_count = holder_count
            self._current_bar_start_slot = bar_start
            self._current_open = price_lamports
            self._current_high = price_lamports
            self._current_low = price_lamports
            self._current_close = price_lamports
            self._current_volume = volume_lamports

    def _flush_current_bar(self) -> None:
        if self._current_bar_start_slot is None:
            return
        bar_close_slot = self._current_bar_start_slot + self.slots_per_bar - 1
        bar = OHLCVBar(
            close_slot=bar_close_slot,
            open_lamports=self._current_open,
            high_lamports=self._current_high,
            low_lamports=max(0, self._current_low if self._current_low < int(2**62) else 0),
            close_lamports=self._current_close,
            volume_lamports=self._current_volume,
            holder_count=self._last_known_holder_count,
        )
        self._bars.append(bar)
        self._current_bar_start_slot = None
        self._current_low = int(2**62)

    def get_bars(self, max_slot: int | None = None) -> list[OHLCVBar]:
        """Return all complete bars (and optionally flush the current partial bar).

        If max_slot is given, only bars with close_slot <= max_slot are returned.
        This is the point-in-time cutoff: we never look at bars from the future.
        """
        # Flush the current partial bar as a snapshot
        complete = list(self._bars)
        # Include the current (partial) bar at its current state
        if self._current_bar_start_slot is not None:
            bar_close_slot = self._current_bar_start_slot + self.slots_per_bar - 1
            partial = OHLCVBar(
                close_slot=bar_close_slot,
                open_lamports=self._current_open,
                high_lamports=self._current_high,
                low_lamports=max(0, self._current_low if self._current_low < int(2**62) else 0),
                close_lamports=self._current_close,
                volume_lamports=self._current_volume,
                holder_count=self._last_known_holder_count,
            )
            complete.append(partial)

        if max_slot is not None:
            complete = [b for b in complete if b.close_slot <= max_slot]
        return complete

    @property
    def bar_count(self) -> int:
        """Number of complete bars in the history."""
        return len(self._bars)

    def reset(self) -> None:
        self._bars = []
        self._current_bar_start_slot = None
        self._current_open = 0
        self._current_high = 0
        self._current_low = int(2**62)
        self._current_close = 0
        self._current_volume = 0
        self._last_known_holder_count = None


# ---------------------------------------------------------------------------
# TA computation (pandas-ta)
# ---------------------------------------------------------------------------


def compute_ta_features(bars: list[OHLCVBar]) -> TAFeatures:
    """Compute RSI/MACD/BB from a list of OHLCV bars.

    Min-history guards emit None + valid=False for insufficient history.
    Never emits garbage values for short history — always honest about data quality.

    SLOW LOOP ONLY: pandas is imported here, not at module level, so this is not
    accidentally called from the SNIPE loop hot path (which forbids pandas).

    Parameters
    ----------
    bars : list[OHLCVBar]
        Point-in-time OHLCV bars with close_slot <= event_time.slot.
        Must be in chronological order (sorted by close_slot ascending).

    Returns
    -------
    TAFeatures
        Typed TA result.  None values indicate min-history guard fired.
    """
    n_bars = len(bars)
    max_bar_slot = bars[-1].close_slot if bars else 0

    # --- Min-history guards: emit None early if insufficient bars ---
    rsi_valid = n_bars >= RSI_MIN_BARS
    macd_valid = n_bars >= MACD_MIN_BARS
    bb_valid = n_bars >= BB_MIN_BARS

    if not (rsi_valid or macd_valid or bb_valid):
        # No indicator has enough history — return all None immediately
        # This is the common case for freshly-minted tokens.
        # COMMENT: RSI/MACD/BB have ZERO signal value on a token that existed
        # for < 26 bars.  Do not try to compute them; do not treat None as zero.
        return TAFeatures(
            rsi=None, macd=None, macd_signal=None, macd_hist=None,
            bb_upper=None, bb_middle=None, bb_lower=None,
            bb_pct_b=None, bb_bandwidth=None,
            rsi_valid=False, macd_valid=False, bb_valid=False,
            bar_count=n_bars, max_bar_slot=max_bar_slot,
        )

    # Import pandas-ta here (not at module level — SNIPE loop must never touch it)
    try:
        import pandas as pd
        import pandas_ta as ta  # type: ignore[import]
    except ImportError as exc:
        logger.error(
            "pandas-ta not available; TA features will be None.  "
            "Install: pip install pandas-ta.  Error: %s", exc
        )
        return TAFeatures(
            rsi=None, macd=None, macd_signal=None, macd_hist=None,
            bb_upper=None, bb_middle=None, bb_lower=None,
            bb_pct_b=None, bb_bandwidth=None,
            rsi_valid=False, macd_valid=False, bb_valid=False,
            bar_count=n_bars, max_bar_slot=max_bar_slot,
        )

    # Build the close price series (int lamports → float for TA math is OK; these
    # are prices, not money balances — the money rule targets exact lamport sums)
    close_prices = [float(b.close_lamports) for b in bars]
    df = pd.DataFrame({"close": close_prices})

    rsi: float | None = None
    macd_val: float | None = None
    macd_sig: float | None = None
    macd_hist_val: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_pct_b: float | None = None
    bb_bandwidth: float | None = None

    # --- RSI ---
    if rsi_valid:
        try:
            rsi_series = ta.rsi(df["close"], length=RSI_LOOKBACK)
            if rsi_series is not None and not rsi_series.empty:
                last_rsi = rsi_series.iloc[-1]
                if last_rsi is not None and not (isinstance(last_rsi, float) and pd.isna(last_rsi)):
                    rsi = float(last_rsi)
                else:
                    rsi_valid = False
        except Exception as exc:
            logger.warning("RSI computation failed: %s", exc)
            rsi_valid = False

    # --- MACD ---
    if macd_valid:
        try:
            macd_df = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
            if macd_df is not None and not macd_df.empty:
                # pandas-ta MACD returns columns: MACD_fast_slow_signal, MACDh_..., MACDs_...
                cols = list(macd_df.columns)
                # Find the MACD line column (starts with "MACD_")
                macd_col = next((c for c in cols if c.startswith("MACD_")), None)
                hist_col = next((c for c in cols if c.startswith("MACDh_")), None)
                sig_col  = next((c for c in cols if c.startswith("MACDs_")), None)

                def _last_float(col: str | None) -> float | None:
                    if col is None:
                        return None
                    v = macd_df[col].iloc[-1]
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return None
                    return float(v)

                macd_val = _last_float(macd_col)
                macd_hist_val = _last_float(hist_col)
                macd_sig = _last_float(sig_col)

                if macd_val is None:
                    macd_valid = False
        except Exception as exc:
            logger.warning("MACD computation failed: %s", exc)
            macd_valid = False

    # --- Bollinger Bands ---
    if bb_valid:
        try:
            bb_df = ta.bbands(df["close"], length=BB_LOOKBACK, std=BB_STD)
            if bb_df is not None and not bb_df.empty:
                cols = list(bb_df.columns)
                upper_col = next((c for c in cols if "BBU_" in c), None)
                mid_col   = next((c for c in cols if "BBM_" in c), None)
                lower_col = next((c for c in cols if "BBL_" in c), None)
                pctb_col  = next((c for c in cols if "BBP_" in c), None)
                bw_col    = next((c for c in cols if "BBB_" in c), None)

                def _last_bb(col: str | None) -> float | None:
                    if col is None:
                        return None
                    v = bb_df[col].iloc[-1]
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return None
                    return float(v)

                bb_upper     = _last_bb(upper_col)
                bb_middle    = _last_bb(mid_col)
                bb_lower     = _last_bb(lower_col)
                bb_pct_b     = _last_bb(pctb_col)
                bb_bandwidth = _last_bb(bw_col)

                if bb_upper is None or bb_middle is None or bb_lower is None:
                    bb_valid = False
        except Exception as exc:
            logger.warning("BB computation failed: %s", exc)
            bb_valid = False

    return TAFeatures(
        rsi=rsi,
        macd=macd_val,
        macd_signal=macd_sig,
        macd_hist=macd_hist_val,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_pct_b=bb_pct_b,
        bb_bandwidth=bb_bandwidth,
        rsi_valid=rsi_valid,
        macd_valid=macd_valid,
        bb_valid=bb_valid,
        bar_count=n_bars,
        max_bar_slot=max_bar_slot,
    )


# ---------------------------------------------------------------------------
# TA feature frame adapter — maps TAFeatures to FeatureFrame TA fields
# ---------------------------------------------------------------------------


def ta_frame_fields(ta: TAFeatures) -> dict:
    """Extract the FeatureFrame-compatible TA fields from a TAFeatures result.

    Returns a dict with exactly the three FeatureFrame TA fields:
        rsi: float | None
        macd: float | None      (the MACD line)
        bb_width: float | None  (bandwidth = (upper - lower) / middle)

    None values propagate when the min-history guard fired.
    """
    # FeatureFrame uses bb_width (bandwidth) as the volatility-regime read.
    # %B is available as bb_pct_b for consumers who need it but is not in FeatureFrame.
    return {
        "rsi": ta.rsi if ta.rsi_valid else None,
        "macd": ta.macd if ta.macd_valid else None,
        "bb_width": ta.bb_bandwidth if ta.bb_valid else None,
    }


# ---------------------------------------------------------------------------
# Price-path tensor for the chart-path / regime model (M2-CP-01)
# ---------------------------------------------------------------------------
#
# This is the honest, point-in-time INPUT the (future, CP-02/CP-04) regime model
# consumes -- a normalized, fixed-length sequence built from the SAME proven
# OHLCVBar history above.  No new price source; no new event stream.
#
# CHANNELS (per bar step, oldest -> newest, RIGHT-aligned so index -1 is the
# bar closest to event_time_slot):
#   log_return    : tanh(log(close_t / close_{t-1}) / LOG_RETURN_SCALE)   in [-1, 1]
#                   (tanh saturates to exactly +/-1.0 for extreme inputs -- still
#                   bounded, never unbounded; NOT a claim of a strict open interval)
#   drawdown      : -((running_peak_close - close_t) / running_peak_close)  in [-1, 0]
#                   0.0  = bar is at (or above) its own running peak so far
#                   -1.0 = complete wipeout relative to the running peak so far
#   holder_delta  : tanh(frac_change(holder_count_t, holder_count_{t-1}) / HOLDER_DELTA_SCALE)
#                   0.0 if EITHER side's holder_count is unknown (NEVER fabricated as a
#                   positive/negative guess -- 0.0 is the honest "no observed change"
#                   value, the same convention MicrostructureFeatures already uses for
#                   effective_sell_tax_bps when there is insufficient data)
#   volume_delta  : tanh(log((volume_t + 1) / (volume_{t-1} + 1)) / VOLUME_DELTA_SCALE)
#
# NORMALIZATION IS POINT-IN-TIME BY CONSTRUCTION (stronger than "fit-as-of-t"):
#   Every channel value is a PURE, FIXED (module-constant, NEVER data-fit) function of
#   at most the current bar and its immediate predecessor.  There is no dataset-wide
#   mean/std/percentile anywhere -- the walk-forward "no global-stat normalization"
#   rule (data-models.md §3.1 point 3) is satisfied trivially because no statistic is
#   ever estimated FROM the data at all; the scale constants (LOG_RETURN_SCALE etc.)
#   are fixed engineering constants declared once, not fit.  This also makes the
#   leakage/causality proof one line: a channel value at bar t cannot change unless
#   bar t or bar t-1 changes.
#
# PRESENCE / CENSORED MASK (never fabricated):
#   The tensor is fixed-length (PRICE_PATH_LENGTH).  A token with fewer bars than that
#   gets LEFT-PADDED with explicit 0.0 entries and presence_mask=0.0 -- the padded
#   slots are CENSORED, not a guess of "no move" (that guess would corrupt a learned
#   model; the mask lets model code treat them as absent).  presence_mask[i] == 1.0
#   means channel[i] is a REAL, computed bar; 0.0 means padding.  bar_count on the
#   result is the TRUE number of bars fed (can exceed PRICE_PATH_LENGTH -- the window
#   only keeps the most recent PRICE_PATH_LENGTH).
#
# POINT-IN-TIME CUTOFF (the leak guard):
#   build_price_path_tensor() filters to bars with close_slot <= event_time_slot BEFORE
#   anything else runs.  A bar from AFTER event_time_slot (a "future bar") is EXCLUDED,
#   full stop -- it never reaches the accumulator, so it cannot perturb any channel
#   value, the mask, bar_count, or max_bar_slot.
#
# INCREMENTAL BY CONSTRUCTION (batch == streaming, one code path):
#   PricePathTensorAccumulator.update_bar() is the ONLY place any channel value is
#   computed.  build_price_path_tensor() (batch) and a live SLOW-loop consumer
#   (streaming, one update_bar() call per new bar) both call this exact method --
#   there is no separate "offline formula".  O(1) state per bar (previous
#   close/volume/holder_count + running peak) plus a fixed-size ring buffer
#   (collections.deque(maxlen=PRICE_PATH_LENGTH)) -- no re-scan of history.
#
# LOOP PLACEMENT: SLOW loop only (assert_price_path_slow_loop_only guard).
#   This tensor feeds the chart-path/regime model (M2-CP-02/03/04), which is SLOW-loop
#   only per the BLUEPRINT triple-loop boundary -- never FAST/SNIPE, no ONNX export,
#   no Rust shim.  build_price_path_tensor() takes an explicit `loop` kwarg and refuses
#   (PricePathLoopViolation) if it is not "slow".
#
# NOT PART OF THE FROZEN FeatureFrame CONTRACT.
#   This tensor is a NEW artifact for the regime model, not a FeatureFrame field.
#   Wiring it into a typed contract is CP-04's job (a proper ADR under
#   .agency/02-architecture/adr/, solana-systems-architect) -- this module only builds
#   the honest, leak-free input; it does not touch aats.contracts.features.

PRICE_PATH_TENSOR_SCHEMA_VERSION = "1.0.0"

PRICE_PATH_LENGTH: int = 60          # fixed sequence length (bars); short history -> left-padded
PRICE_PATH_CHANNELS: tuple[str, ...] = ("log_return", "drawdown", "holder_delta", "volume_delta")

# Fixed (NEVER fit-from-data) squash-scale constants -- see NORMALIZATION note above.
LOG_RETURN_SCALE: float = 0.20       # tanh squash scale for a bar-over-bar log return
HOLDER_DELTA_SCALE: float = 0.10     # tanh squash scale for a fractional holder-count change
VOLUME_DELTA_SCALE: float = 1.0      # tanh squash scale for a log volume ratio

PRICE_PATH_LOOP: str = "slow"


class PricePathLoopViolation(RuntimeError):
    """Raised if the price-path tensor builder is asked to run on FAST/SNIPE.

    The chart-path/regime model this tensor feeds is SLOW-loop only (BLUEPRINT
    triple-loop boundary) -- no ONNX export, no Rust shim.  This guard makes a
    mis-wire a loud build/test failure, not a silent latency bomb.
    """


def assert_price_path_slow_loop_only(loop: str) -> None:
    """Fail loudly if a caller tries to build the price-path tensor on a non-slow loop."""
    if loop != PRICE_PATH_LOOP:
        raise PricePathLoopViolation(
            f"price-path tensor is SLOW-loop only (loop={PRICE_PATH_LOOP!r}); refused to "
            f"run on loop={loop!r}.  The chart-path/regime model it feeds never runs on "
            "the FAST/SNIPE path (BLUEPRINT triple-loop boundary)."
        )


class PricePathOrderError(ValueError):
    """Raised if bars are fed to the accumulator out of increasing close_slot order.

    Point-in-time correctness requires a strictly increasing bar sequence -- an
    out-of-order or duplicate close_slot would let a later 'past' bar rewrite a
    channel value that has already been computed and (in the streaming path) already
    observed downstream.  Loud failure, not a silent overwrite.
    """


@dataclass(frozen=True)
class PricePathTensor:
    """Fixed-length, point-in-time, normalized price-path tensor (M2-CP-01).

    The honest INPUT for the (future) chart-path / regime model.  Every field is
    right-aligned: index -1 is the bar closest to event_time_slot; index 0 is the
    oldest bar in the window (or a CENSORED pad if history is shorter than
    PRICE_PATH_LENGTH).

    All four channel tuples and presence_mask have length == `length`.  Values are
    bounded, fixed-scale-normalized floats (never money) -- see the module-level
    NORMALIZATION note above for the exact per-channel formula.
    """

    log_return: tuple[float, ...]
    drawdown: tuple[float, ...]
    holder_delta: tuple[float, ...]
    volume_delta: tuple[float, ...]
    presence_mask: tuple[float, ...]   # 1.0 = real bar, 0.0 = CENSORED/padding

    length: int              # the fixed sequence length (== len of every tuple above)
    bar_count: int           # TRUE number of real bars fed (may exceed `length`)
    max_bar_slot: int        # highest close_slot that contributed (provenance)
    event_time_slot: int     # the point-in-time cutoff this tensor is valid as-of


@dataclass
class PricePathTensorAccumulator:
    """Incrementally builds a PricePathTensor from OHLCVBar observations, one bar at a time.

    ONE code path for batch and streaming (parity requirement): every channel value is
    produced by update_bar(); build_price_path_tensor() (the batch/offline path) and a
    live SLOW-loop consumer (the streaming/online path) both call this exact method, so
    their outputs are bit-identical by construction, not by convention.

    State is O(1) per bar (previous close/volume/holder_count + a running peak) plus a
    fixed-size deque(maxlen=length) ring buffer per channel -- no re-scan of history.
    """

    length: int = PRICE_PATH_LENGTH

    _prev_close: int | None = field(default=None, repr=False)
    _prev_volume: int | None = field(default=None, repr=False)
    _prev_holder_count: int | None = field(default=None, repr=False)
    _running_peak: int = field(default=0, repr=False)
    _last_seen_close_slot: int | None = field(default=None, repr=False)
    _max_bar_slot: int = field(default=0, repr=False)
    _bar_count: int = field(default=0, repr=False)

    _log_return: deque = field(init=False, repr=False)
    _drawdown: deque = field(init=False, repr=False)
    _holder_delta: deque = field(init=False, repr=False)
    _volume_delta: deque = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError(f"length must be > 0, got {self.length}")
        self._log_return = deque(maxlen=self.length)
        self._drawdown = deque(maxlen=self.length)
        self._holder_delta = deque(maxlen=self.length)
        self._volume_delta = deque(maxlen=self.length)

    def update_bar(self, bar: OHLCVBar) -> None:
        """Consume ONE bar (must have close_slot strictly greater than the last one fed).

        This is the SINGLE place any price-path channel value is computed -- batch and
        streaming share it, so their outputs are bit-identical.
        """
        if (
            self._last_seen_close_slot is not None
            and bar.close_slot <= self._last_seen_close_slot
        ):
            raise PricePathOrderError(
                f"bar.close_slot={bar.close_slot} must be strictly greater than the last "
                f"bar fed (close_slot={self._last_seen_close_slot}).  Bars must be fed in "
                "strictly increasing close_slot order -- point-in-time correctness "
                "requires it."
            )

        close = bar.close_lamports
        volume = bar.volume_lamports
        holder = bar.holder_count

        # --- log-return: bar-over-bar close-to-close, tanh-squashed by a FIXED scale ---
        if self._prev_close is not None and self._prev_close > 0 and close > 0:
            raw_log_return = math.log(close / self._prev_close)
        else:
            raw_log_return = 0.0  # genesis bar: no prior close to compare against
        self._log_return.append(math.tanh(raw_log_return / LOG_RETURN_SCALE))

        # --- drawdown-from-peak: expanding (causal) running max, never a centered window ---
        if close > self._running_peak:
            self._running_peak = close
        if self._running_peak > 0:
            drawdown_frac = (self._running_peak - close) / self._running_peak
        else:
            drawdown_frac = 0.0
        drawdown_frac = min(1.0, max(0.0, drawdown_frac))
        self._drawdown.append(-drawdown_frac)

        # --- holder-count delta: 0.0 (neutral, NOT fabricated) if either side unknown ---
        if (
            holder is not None
            and self._prev_holder_count is not None
            and self._prev_holder_count > 0
        ):
            raw_holder_delta = (holder - self._prev_holder_count) / self._prev_holder_count
        else:
            raw_holder_delta = 0.0
        self._holder_delta.append(math.tanh(raw_holder_delta / HOLDER_DELTA_SCALE))

        # --- volume delta: log-ratio (+1 smoothing survives zero-volume bars) ---
        if self._prev_volume is not None:
            raw_volume_delta = math.log((volume + 1) / (self._prev_volume + 1))
        else:
            raw_volume_delta = 0.0
        self._volume_delta.append(math.tanh(raw_volume_delta / VOLUME_DELTA_SCALE))

        # --- advance "previous" state strictly AFTER computing this bar's channels ---
        self._prev_close = close
        self._prev_volume = volume
        if holder is not None:
            self._prev_holder_count = holder

        self._last_seen_close_slot = bar.close_slot
        if bar.close_slot > self._max_bar_slot:
            self._max_bar_slot = bar.close_slot
        self._bar_count += 1

    def to_tensor(self, event_time_slot: int) -> PricePathTensor:
        """Materialize the current ring-buffer state into a fixed-length PricePathTensor.

        Left-pads with CENSORED (0.0-valued, mask=0.0) entries if fewer than `length`
        real bars have been fed -- never fabricates a value for missing history.
        """
        n_real = len(self._log_return)   # == min(true bar_count, length), ring-buffer-capped
        n_pad = self.length - n_real
        zero_pad = [0.0] * n_pad
        mask = [0.0] * n_pad + [1.0] * n_real

        return PricePathTensor(
            log_return=tuple(zero_pad + list(self._log_return)),
            drawdown=tuple(zero_pad + list(self._drawdown)),
            holder_delta=tuple(zero_pad + list(self._holder_delta)),
            volume_delta=tuple(zero_pad + list(self._volume_delta)),
            presence_mask=tuple(mask),
            length=self.length,
            bar_count=self._bar_count,
            max_bar_slot=self._max_bar_slot,
            event_time_slot=event_time_slot,
        )

    def reset(self) -> None:
        self._prev_close = None
        self._prev_volume = None
        self._prev_holder_count = None
        self._running_peak = 0
        self._last_seen_close_slot = None
        self._max_bar_slot = 0
        self._bar_count = 0
        self._log_return.clear()
        self._drawdown.clear()
        self._holder_delta.clear()
        self._volume_delta.clear()


def build_price_path_tensor(
    bars: list[OHLCVBar],
    event_time_slot: int,
    *,
    loop: str = PRICE_PATH_LOOP,
    length: int = PRICE_PATH_LENGTH,
) -> PricePathTensor:
    """Batch path: assemble a PricePathTensor from a bar history, anchored at event_time_slot.

    THE LEAK GUARD: bars are filtered to close_slot <= event_time_slot BEFORE anything
    else runs.  A "future" bar (close_slot > event_time_slot) is excluded outright -- it
    never reaches the accumulator and cannot perturb any channel value, the mask,
    bar_count, or max_bar_slot.  This reuses the SAME proven bar assembly contract
    OHLCVBarAssembler.get_bars(max_slot=...) already upholds -- no new price source is
    introduced.

    Reuses PricePathTensorAccumulator.update_bar() as its ONLY computation step, so this
    batch path and a live streaming SLOW-loop consumer are bit-identical by construction.

    Parameters
    ----------
    bars : list[OHLCVBar]
        The full bar history available (may include bars past event_time_slot; they
        are filtered out here, not by the caller).
    event_time_slot : int
        The point-in-time cutoff.  Only bars with close_slot <= event_time_slot count.
    loop : str
        Must be "slow" (default).  A fast/snipe loop raises PricePathLoopViolation --
        the chart-path/regime model this tensor feeds is SLOW-loop only.
    length : int
        Fixed output sequence length (default PRICE_PATH_LENGTH).  Short history is
        left-padded with CENSORED (0.0, mask=0.0) entries, never fabricated values.

    Returns
    -------
    PricePathTensor
    """
    assert_price_path_slow_loop_only(loop)

    pit_bars = sorted(
        (b for b in bars if b.close_slot <= event_time_slot),
        key=lambda b: b.close_slot,
    )

    acc = PricePathTensorAccumulator(length=length)
    for bar in pit_bars:
        acc.update_bar(bar)
    return acc.to_tensor(event_time_slot=event_time_slot)


def price_path_tensor_to_arrays(tensor: PricePathTensor):
    """Optional numpy view of a PricePathTensor: (channels, mask) as float32 ndarrays.

    numpy is imported HERE, not at module level (mirrors the pandas-ta deferred-import
    discipline above) -- this keeps the pure-Python accumulator path importable without
    numpy, and this helper is SLOW-loop-only tooling, never a hot-path dependency.

    Returns
    -------
    channels : np.ndarray, shape (tensor.length, 4), dtype float32
        Columns in PRICE_PATH_CHANNELS order: log_return, drawdown, holder_delta, volume_delta.
    mask : np.ndarray, shape (tensor.length,), dtype float32
        1.0 = real bar, 0.0 = CENSORED/padding.
    """
    import numpy as np

    channels = np.stack(
        [
            np.asarray(tensor.log_return, dtype=np.float32),
            np.asarray(tensor.drawdown, dtype=np.float32),
            np.asarray(tensor.holder_delta, dtype=np.float32),
            np.asarray(tensor.volume_delta, dtype=np.float32),
        ],
        axis=1,
    )
    mask = np.asarray(tensor.presence_mask, dtype=np.float32)
    return channels, mask
