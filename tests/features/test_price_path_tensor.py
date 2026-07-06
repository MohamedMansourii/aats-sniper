"""Tests for the price-path tensor (M2-CP-01 — chart-path/regime-model INPUT).

Self-check criteria (mirrors the M2-CP-01 brief):
  1. Tensor shape / normalization is correct (fixed length, bounded channels).
  2. LEAK TEST: a future bar (close_slot > event_time.slot) is EXCLUDED —
     mutating it arbitrarily leaves the tensor byte-identical (future-mutation
     invariance, the causality proof required at handoff).
  3. Short-history -> masked (presence_mask=0.0, value=0.0), never fabricated.
  4. Batch (build_price_path_tensor) == streaming (PricePathTensorAccumulator
     fed one bar at a time) parity.
  5. SLOW-loop-only guard (assert_price_path_slow_loop_only / PricePathLoopViolation).
  6. Bar-order guard (PricePathOrderError) — no silent overwrite of a computed value.
  7. OHLCVBarAssembler.holder_count extension is backward compatible and
     forward-fills only from PAST observations.
"""

from __future__ import annotations

import math

import pytest

from aats.features.ta import (
    PRICE_PATH_LENGTH,
    PRICE_PATH_LOOP,
    OHLCVBar,
    OHLCVBarAssembler,
    PricePathLoopViolation,
    PricePathOrderError,
    PricePathTensor,
    PricePathTensorAccumulator,
    build_price_path_tensor,
    price_path_tensor_to_arrays,
)

LAMPORTS_PER_SOL = 1_000_000_000


def _bar(
    close_slot: int,
    close_lamports: int = 1_000_000,
    volume_lamports: int = LAMPORTS_PER_SOL,
    holder_count: int | None = None,
) -> OHLCVBar:
    """Build a minimal OHLCVBar for tensor tests (open=high=low=close, unless noted)."""
    return OHLCVBar(
        close_slot=close_slot,
        open_lamports=close_lamports,
        high_lamports=close_lamports,
        low_lamports=close_lamports,
        close_lamports=close_lamports,
        volume_lamports=volume_lamports,
        holder_count=holder_count,
    )


def _rising_bars(n: int, close_slot_step: int = 10, start_price: int = 1_000_000) -> list[OHLCVBar]:
    """n bars with strictly rising close price (each bar is a new peak)."""
    return [
        _bar(close_slot=i * close_slot_step, close_lamports=start_price + i * 10_000)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. Tensor shape / normalization
# ---------------------------------------------------------------------------


class TestTensorShape:
    """The tensor is always fixed-length, regardless of how much history exists."""

    def test_default_length_with_ample_history(self):
        bars = _rising_bars(PRICE_PATH_LENGTH + 40)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot)
        assert tensor.length == PRICE_PATH_LENGTH
        assert len(tensor.log_return) == PRICE_PATH_LENGTH
        assert len(tensor.drawdown) == PRICE_PATH_LENGTH
        assert len(tensor.holder_delta) == PRICE_PATH_LENGTH
        assert len(tensor.volume_delta) == PRICE_PATH_LENGTH
        assert len(tensor.presence_mask) == PRICE_PATH_LENGTH
        # More bars were fed than the window keeps -> ALL slots are real.
        assert tensor.bar_count == PRICE_PATH_LENGTH + 40
        assert all(m == 1.0 for m in tensor.presence_mask)

    def test_custom_length(self):
        bars = _rising_bars(5)
        tensor = build_price_path_tensor(
            bars, event_time_slot=bars[-1].close_slot, length=10
        )
        assert tensor.length == 10
        assert len(tensor.log_return) == 10
        assert tensor.bar_count == 5

    def test_channels_are_python_floats(self):
        bars = _rising_bars(5)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot, length=8)
        for channel in (tensor.log_return, tensor.drawdown, tensor.holder_delta, tensor.volume_delta):
            for v in channel:
                assert isinstance(v, float)

    def test_no_nan_or_inf_ever(self):
        """Even a full-wipeout (close=0) bar must never produce NaN/inf."""
        bars = [
            _bar(close_slot=0, close_lamports=1_000_000),
            _bar(close_slot=10, close_lamports=0),          # rug: price to zero
            _bar(close_slot=20, close_lamports=500_000),    # partial recovery
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=20, length=8)
        for channel in (tensor.log_return, tensor.drawdown, tensor.holder_delta, tensor.volume_delta):
            for v in channel:
                assert math.isfinite(v), f"non-finite value {v} in channel"


class TestNormalizationBounds:
    """Every channel is a FIXED (never data-fit) pointwise function -> always bounded.

    tanh saturates to exactly +/-1.0 for extreme inputs (float64 precision) -- that is
    still "bounded", so the extreme-input tests below use closed [-1, 1] bounds.  The
    "moderate move" tests are the MUTATION-MEANINGFUL half: they prove the value is
    actually tanh-squashed (a strictly-interior float), not a bug that just clamps the
    sign to exactly +/-1.0 regardless of magnitude.
    """

    def test_log_return_bounded_for_extreme_move(self):
        bars = [
            _bar(close_slot=0, close_lamports=1),
            _bar(close_slot=10, close_lamports=10_000_000_000),  # huge jump
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=4)
        assert all(-1.0 <= v <= 1.0 for v in tensor.log_return)

    def test_log_return_not_saturated_for_moderate_move(self):
        """MUTATION-MEANINGFUL: a bug that clamps to sign() instead of tanh() fails this
        (it would return exactly 1.0, not a strictly-interior value)."""
        bars = [
            _bar(close_slot=0, close_lamports=1_000_000),
            _bar(close_slot=10, close_lamports=1_100_000),  # +10% move
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=2)
        assert 0.0 < tensor.log_return[-1] < 1.0

    def test_volume_delta_bounded_for_extreme_move(self):
        bars = [
            _bar(close_slot=0, volume_lamports=1),
            _bar(close_slot=10, volume_lamports=10_000_000_000_000),
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=4)
        assert all(-1.0 <= v <= 1.0 for v in tensor.volume_delta)

    def test_volume_delta_not_saturated_for_moderate_move(self):
        bars = [
            _bar(close_slot=0, volume_lamports=1_000_000_000),
            _bar(close_slot=10, volume_lamports=1_100_000_000),
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=2)
        assert 0.0 < tensor.volume_delta[-1] < 1.0

    def test_holder_delta_bounded_for_extreme_move(self):
        bars = [
            _bar(close_slot=0, holder_count=1),
            _bar(close_slot=10, holder_count=1_000_000),
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=4)
        assert all(-1.0 <= v <= 1.0 for v in tensor.holder_delta)

    def test_holder_delta_not_saturated_for_moderate_move(self):
        bars = [
            _bar(close_slot=0, holder_count=100),
            _bar(close_slot=10, holder_count=110),
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=2)
        assert 0.0 < tensor.holder_delta[-1] < 1.0

    def test_drawdown_bounded_and_zero_at_running_peak(self):
        """Strictly rising prices: every bar IS the new peak -> drawdown == 0.0 always."""
        bars = _rising_bars(6)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot, length=6)
        assert all(v == 0.0 for v in tensor.drawdown)
        assert all(-1.0 <= v <= 0.0 for v in tensor.drawdown)

    def test_drawdown_reflects_pullback(self):
        """Rise then fall: the falling bar's drawdown must be strictly negative.

        MUTATION-MEANINGFUL: a sign-flip bug (drawdown = +frac instead of -frac) makes
        this assertion fail (it would be positive), and a "drawdown never updates" bug
        makes it fail too (it would stay 0.0).
        """
        bars = [
            _bar(close_slot=0, close_lamports=1_000_000),
            _bar(close_slot=10, close_lamports=2_000_000),   # new peak
            _bar(close_slot=20, close_lamports=1_000_000),   # 50% pullback from peak
        ]
        tensor = build_price_path_tensor(bars, event_time_slot=20, length=3)
        # index -1 is bar close_slot=20 (the pullback bar)
        assert tensor.drawdown[-1] < 0.0
        assert tensor.drawdown[-1] == pytest.approx(-0.5, abs=1e-9)
        # the peak bar itself (index -2) is still at its own running peak
        assert tensor.drawdown[-2] == 0.0

    def test_log_return_sign_matches_price_direction(self):
        """MUTATION-MEANINGFUL: a sign-flip in log-return computation fails this."""
        rising = build_price_path_tensor(_rising_bars(4), event_time_slot=30, length=4)
        assert rising.log_return[-1] > 0.0

        falling_bars = [
            _bar(close_slot=0, close_lamports=2_000_000),
            _bar(close_slot=10, close_lamports=1_000_000),
        ]
        falling = build_price_path_tensor(falling_bars, event_time_slot=10, length=2)
        assert falling.log_return[-1] < 0.0


# ---------------------------------------------------------------------------
# 2. Short-history -> masked, NEVER fabricated
# ---------------------------------------------------------------------------


class TestPresenceMaskShortHistory:
    def test_short_history_left_padded_and_masked(self):
        bars = _rising_bars(3)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot, length=10)
        assert tensor.bar_count == 3
        # Left 7 slots are CENSORED padding; right 3 are real.
        assert tensor.presence_mask == (0.0,) * 7 + (1.0,) * 3
        # Padded region is EXPLICIT 0.0 -- never a fabricated non-zero guess.
        assert tensor.log_return[:7] == (0.0,) * 7
        assert tensor.drawdown[:7] == (0.0,) * 7
        assert tensor.holder_delta[:7] == (0.0,) * 7
        assert tensor.volume_delta[:7] == (0.0,) * 7

    def test_zero_bars_fully_censored(self):
        tensor = build_price_path_tensor([], event_time_slot=1000, length=6)
        assert tensor.bar_count == 0
        assert tensor.max_bar_slot == 0
        assert tensor.presence_mask == (0.0,) * 6
        assert tensor.log_return == (0.0,) * 6
        assert tensor.drawdown == (0.0,) * 6
        assert tensor.holder_delta == (0.0,) * 6
        assert tensor.volume_delta == (0.0,) * 6

    def test_exact_length_history_no_padding(self):
        bars = _rising_bars(6)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot, length=6)
        assert tensor.bar_count == 6
        assert tensor.presence_mask == (1.0,) * 6


# ---------------------------------------------------------------------------
# 3. LEAK TEST: a future bar (close_slot > event_time.slot) is EXCLUDED
# ---------------------------------------------------------------------------


class TestFutureBarExcluded:
    """The core causality proof: perturbing event_time > t input leaves value at t unchanged."""

    def test_future_bar_entirely_excluded(self):
        bars = _rising_bars(5)  # close_slots 0,10,20,30,40
        cutoff = 30
        future_bar = _bar(close_slot=100, close_lamports=999_999_999, volume_lamports=1)

        without_future = build_price_path_tensor(bars, event_time_slot=cutoff, length=8)
        with_future = build_price_path_tensor(bars + [future_bar], event_time_slot=cutoff, length=8)

        assert with_future == without_future

    def test_future_bar_mutation_invariance(self):
        """Perturbing the future bar's price/volume/holder ARBITRARILY changes nothing at t."""
        bars = _rising_bars(5)
        cutoff = 30

        baseline = build_price_path_tensor(bars, event_time_slot=cutoff, length=8)

        for mutant_price, mutant_vol, mutant_holders in (
            (1, 1, 1),
            (10**15, 10**15, 10**9),
            (0, 0, 0),
        ):
            mutant_future = _bar(
                close_slot=999,
                close_lamports=mutant_price,
                volume_lamports=mutant_vol,
                holder_count=mutant_holders,
            )
            mutated = build_price_path_tensor(
                bars + [mutant_future], event_time_slot=cutoff, length=8
            )
            assert mutated == baseline, "a future bar mutation changed a point-in-time tensor"

    def test_max_bar_slot_excludes_future(self):
        bars = _rising_bars(5)  # close_slots 0,10,20,30,40
        cutoff = 30
        future_bar = _bar(close_slot=100)
        tensor = build_price_path_tensor(bars + [future_bar], event_time_slot=cutoff, length=8)
        # highest close_slot <= cutoff=30 is the in-window bar at slot 30 -- NOT the
        # future bar at slot 100 and NOT the excluded bar at slot 40.
        assert tensor.max_bar_slot == 30

    def test_bar_at_exactly_cutoff_is_included(self):
        """close_slot == event_time.slot is INCLUDED (the boundary is <=, not <)."""
        bars = [_bar(close_slot=0), _bar(close_slot=10)]
        tensor = build_price_path_tensor(bars, event_time_slot=10, length=4)
        assert tensor.bar_count == 2
        assert tensor.max_bar_slot == 10


# ---------------------------------------------------------------------------
# 4. Batch == streaming parity
# ---------------------------------------------------------------------------


class TestBatchStreamingParity:
    def test_batch_matches_incremental_accumulator(self):
        bars = _rising_bars(75, close_slot_step=10)
        cutoff = bars[-1].close_slot

        batch = build_price_path_tensor(bars, event_time_slot=cutoff, length=PRICE_PATH_LENGTH)

        acc = PricePathTensorAccumulator(length=PRICE_PATH_LENGTH)
        for bar in bars:
            acc.update_bar(bar)
        streaming = acc.to_tensor(event_time_slot=cutoff)

        assert streaming == batch

    def test_batch_matches_incremental_with_holder_counts(self):
        bars = [
            _bar(close_slot=i * 10, close_lamports=1_000_000 + i * 500, holder_count=50 + i)
            for i in range(30)
        ]
        cutoff = bars[-1].close_slot

        batch = build_price_path_tensor(bars, event_time_slot=cutoff, length=20)

        acc = PricePathTensorAccumulator(length=20)
        for bar in bars:
            acc.update_bar(bar)
        streaming = acc.to_tensor(event_time_slot=cutoff)

        assert streaming == batch


# ---------------------------------------------------------------------------
# 5. SLOW-loop-only guard
# ---------------------------------------------------------------------------


class TestSlowLoopGuard:
    def test_default_loop_is_slow_and_succeeds(self):
        tensor = build_price_path_tensor([], event_time_slot=0)
        assert isinstance(tensor, PricePathTensor)

    def test_fast_loop_raises(self):
        with pytest.raises(PricePathLoopViolation):
            build_price_path_tensor([], event_time_slot=0, loop="fast")

    def test_snipe_loop_raises(self):
        with pytest.raises(PricePathLoopViolation):
            build_price_path_tensor([], event_time_slot=0, loop="snipe")

    def test_price_path_loop_constant_is_slow(self):
        assert PRICE_PATH_LOOP == "slow"


# ---------------------------------------------------------------------------
# 6. Bar-order guard
# ---------------------------------------------------------------------------


class TestOrderGuard:
    def test_out_of_order_bar_raises(self):
        acc = PricePathTensorAccumulator(length=8)
        acc.update_bar(_bar(close_slot=10))
        with pytest.raises(PricePathOrderError):
            acc.update_bar(_bar(close_slot=5))

    def test_duplicate_close_slot_raises(self):
        acc = PricePathTensorAccumulator(length=8)
        acc.update_bar(_bar(close_slot=10))
        with pytest.raises(PricePathOrderError):
            acc.update_bar(_bar(close_slot=10))

    def test_zero_or_negative_length_rejected(self):
        with pytest.raises(ValueError):
            PricePathTensorAccumulator(length=0)


# ---------------------------------------------------------------------------
# 7. OHLCVBarAssembler.holder_count extension
# ---------------------------------------------------------------------------


class TestAssemblerHolderCountExtension:
    def test_backward_compatible_without_holder_count(self):
        """Existing (pre-M2-CP-01) call sites that never pass holder_count still work."""
        asm = OHLCVBarAssembler(slots_per_bar=10)
        asm.observe(slot=0, price_lamports=1_000_000, volume_lamports=10)
        bars = asm.get_bars()
        assert bars[0].holder_count is None

    def test_holder_count_persists_forward_across_bars(self):
        """A holder_count observed in an earlier bar carries forward (PAST -> future bar OK)."""
        asm = OHLCVBarAssembler(slots_per_bar=10)
        asm.observe(slot=0, price_lamports=1_000_000, volume_lamports=10, holder_count=42)
        asm.observe(slot=15, price_lamports=1_100_000, volume_lamports=5)  # no holder_count here
        bars = asm.get_bars()
        assert len(bars) == 2
        assert bars[0].holder_count == 42
        assert bars[1].holder_count == 42  # carried forward, not fabricated / not None

    def test_holder_count_updates_when_provided(self):
        asm = OHLCVBarAssembler(slots_per_bar=10)
        asm.observe(slot=0, price_lamports=1_000_000, volume_lamports=10, holder_count=10)
        asm.observe(slot=15, price_lamports=1_100_000, volume_lamports=5, holder_count=20)
        bars = asm.get_bars()
        assert bars[0].holder_count == 10
        assert bars[1].holder_count == 20

    def test_holder_count_must_be_int(self):
        asm = OHLCVBarAssembler(slots_per_bar=10)
        with pytest.raises(TypeError):
            asm.observe(slot=0, price_lamports=1_000_000, volume_lamports=10, holder_count=1.5)  # type: ignore

    def test_reset_clears_holder_count(self):
        asm = OHLCVBarAssembler(slots_per_bar=10)
        asm.observe(slot=0, price_lamports=1_000_000, volume_lamports=10, holder_count=99)
        asm.reset()
        asm.observe(slot=0, price_lamports=1_000_000, volume_lamports=10)
        bars = asm.get_bars()
        assert bars[0].holder_count is None


# ---------------------------------------------------------------------------
# 8. numpy conversion helper (optional tensor view)
# ---------------------------------------------------------------------------


class TestPricePathTensorToArrays:
    def test_shapes(self):
        np = pytest.importorskip("numpy")
        bars = _rising_bars(5)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot, length=8)
        channels, mask = price_path_tensor_to_arrays(tensor)
        assert channels.shape == (8, 4)
        assert mask.shape == (8,)
        assert channels.dtype == np.float32
        assert mask.dtype == np.float32

    def test_mask_matches_presence_mask(self):
        bars = _rising_bars(3)
        tensor = build_price_path_tensor(bars, event_time_slot=bars[-1].close_slot, length=8)
        _, mask = price_path_tensor_to_arrays(tensor)
        assert list(mask) == list(tensor.presence_mask)
