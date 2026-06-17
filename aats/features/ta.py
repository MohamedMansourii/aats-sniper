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
"""

from __future__ import annotations

import logging
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

    def observe(self, slot: int, price_lamports: int, volume_lamports: int) -> None:
        """Add a price/volume observation at a given slot.

        price_lamports : int (lamports — not float)
        volume_lamports : int (lamports — not float)
        slot : int

        All int, no float.
        """
        if not isinstance(price_lamports, int):
            raise TypeError(f"price_lamports must be int, got {type(price_lamports)}")
        if not isinstance(volume_lamports, int):
            raise TypeError(f"volume_lamports must be int, got {type(volume_lamports)}")
        if not isinstance(slot, int):
            raise TypeError(f"slot must be int, got {type(slot)}")

        bar_start = (slot // self.slots_per_bar) * self.slots_per_bar

        if self._current_bar_start_slot is None:
            # First observation
            self._current_bar_start_slot = bar_start
            self._current_open = price_lamports
            self._current_high = price_lamports
            self._current_low = price_lamports
            self._current_close = price_lamports
            self._current_volume = volume_lamports
            return

        if bar_start == self._current_bar_start_slot:
            # Same bar
            if price_lamports > self._current_high:
                self._current_high = price_lamports
            if price_lamports < self._current_low:
                self._current_low = price_lamports
            self._current_close = price_lamports
            self._current_volume += volume_lamports
        else:
            # New bar: flush current bar, start new one
            self._flush_current_bar()
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
