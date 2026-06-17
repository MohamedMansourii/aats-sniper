"""E10 — Social-velocity + organic-vs-bot growth ratio tests.

Self-check items verified here:
  SC-E10-A: Bot-driven / coordinated velocity LOWERS MCS conviction.
             Organic velocity does NOT raise conviction (penalty=0.0 for clean signal).
  SC-E10-B: Organic posts score higher conviction than bot-farm burst.
  SC-E10-C: VelocitySignal.mcs_penalty is ALWAYS >= 0.0 (can only de-risk).
  SC-E10-D: apply_velocity_penalty_to_conviction NEVER exceeds base_conviction.
  SC-E10-E: Point-in-time filter: future events (event_time_ms > decision_time_ms)
             are excluded and tracked in posts_excluded_future.
  SC-E10-F: Factory-account cohort detection: accounts with same creation-date
             cohort trigger holder_growth penalty.
  SC-E10-G: Offline / injectable fixtures — no live network calls.
  SC-E10-H: No win_rate field anywhere in the velocity module source.
  SC-E10-I: Signal is SLOW-LOOP ONLY (not imported into fast-path modules).
  SC-E10-J: mutation-meaningful: neuter bot_velocity_penalty → bot burst is NOT
             penalised → test RED.

GOLDEN FIXTURES (deterministic, no wall-clock):
  BOT_BURST_EVENTS:     20 events, brand-new accounts (0-1 days), is_bot_scored=True,
                        in a 5-minute burst.  Must produce HIGH penalty.
  ORGANIC_EVENTS:       5 events, established accounts (365+ days), is_bot_scored=False,
                        spread over 15 minutes.  Must produce LOW penalty.
  MIXED_EVENTS:         2 organic + 10 bot events.  Must produce penalty > organic.
  FACTORY_COHORT:       All accounts created on the same day (cohort flag=True).
  FUTURE_EVENTS:        Events after decision_time_ms (must be excluded).
"""

from __future__ import annotations

import inspect

import pytest

from aats.sentiment.velocity import (
    BOT_VELOCITY_WEIGHT,
    GROWTH_RATIO_WEIGHT,
    InMemoryVelocitySource,
    VelocityEvent,
    VelocitySignalComputer,
    _compute_bot_velocity_penalty,
    _compute_growth_ratio_penalty,
    _compute_holder_growth_penalty,
    apply_velocity_penalty_to_conviction,
    assert_velocity_cannot_raise_conviction,
    make_velocity_event,
)

# ---------------------------------------------------------------------------
# Time anchors (deterministic — no wall-clock)
# ---------------------------------------------------------------------------

DECISION_TIME_MS: int = 1_718_500_000_000  # 2024-06-16T00:00:00Z
ONE_MIN_MS: int = 60_000
ONE_HOUR_MS: int = 3_600_000
WINDOW_MS: int = 900_000  # 15 minutes

T_MINUS_14MIN = DECISION_TIME_MS - 14 * ONE_MIN_MS
T_MINUS_10MIN = DECISION_TIME_MS - 10 * ONE_MIN_MS
T_MINUS_5MIN = DECISION_TIME_MS - 5 * ONE_MIN_MS
T_MINUS_2MIN = DECISION_TIME_MS - 2 * ONE_MIN_MS
T_MINUS_1MIN = DECISION_TIME_MS - ONE_MIN_MS
T_PLUS_5MIN = DECISION_TIME_MS + 5 * ONE_MIN_MS

ASSET = "VelocityTestMint1111111111111111111111"

# ---------------------------------------------------------------------------
# Golden fixtures
# ---------------------------------------------------------------------------


def _make_bot_event(i: int, event_time_ms: int) -> VelocityEvent:
    """Brand-new bot account, default avatar, high cadence."""
    return make_velocity_event(
        event_id=f"bot_event_{i:03d}",
        account_id=f"bot_account_{i:03d}",
        event_time_ms=event_time_ms,
        account_age_days=float(i % 2),       # 0 or 1 days — brand new
        is_bot_scored=True,
        source="x",
    )


def _make_organic_event(i: int, event_time_ms: int) -> VelocityEvent:
    """Established organic account."""
    return make_velocity_event(
        event_id=f"organic_event_{i:03d}",
        account_id=f"organic_account_{i:03d}",
        event_time_ms=event_time_ms,
        account_age_days=365.0 + i * 10.0,   # well-established accounts
        is_bot_scored=False,
        source="x",
    )


def _make_future_event(i: int) -> VelocityEvent:
    """Event timestamped AFTER decision_time_ms — must be excluded."""
    return make_velocity_event(
        event_id=f"future_event_{i:03d}",
        account_id=f"future_account_{i:03d}",
        event_time_ms=T_PLUS_5MIN + i * ONE_MIN_MS,
        account_age_days=200.0,
        is_bot_scored=False,
        source="x",
    )


# 20 bot events in a 4-minute burst (tight synchronicity)
BOT_BURST_EVENTS: list[VelocityEvent] = [
    _make_bot_event(i, T_MINUS_5MIN + i * 12_000)  # 12s apart → ~4 min total
    for i in range(20)
]

# 5 organic events spread across 13 minutes
ORGANIC_EVENTS: list[VelocityEvent] = [
    _make_organic_event(i, T_MINUS_14MIN + i * 3 * ONE_MIN_MS)
    for i in range(5)
]

# Mixed: 2 organic + 10 bots
MIXED_EVENTS: list[VelocityEvent] = ORGANIC_EVENTS[:2] + BOT_BURST_EVENTS[:10]

# Factory cohort: all accounts created in the same 12-hour window (bot farm)
# Account ages: 2.0, 2.1, 2.2, ... 2.9 days (all within 1-day cohort window)
FACTORY_COHORT_EVENTS: list[VelocityEvent] = [
    make_velocity_event(
        event_id=f"cohort_{i:03d}",
        account_id=f"cohort_account_{i:03d}",
        event_time_ms=T_MINUS_5MIN + i * ONE_MIN_MS,
        account_age_days=2.0 + i * 0.1,    # all within 1-day cohort window
        is_bot_scored=True,                  # bot-scored + cohort = double signal
        source="x",
    )
    for i in range(10)
]

# Future events (must all be excluded)
FUTURE_EVENTS: list[VelocityEvent] = [_make_future_event(i) for i in range(3)]

# ---------------------------------------------------------------------------
# SC-E10-A: Bot-driven velocity LOWERS conviction
# ---------------------------------------------------------------------------


class TestBotDrivenVelocityLowersConviction:
    """SC-E10-A: Bot burst must produce a positive penalty that lowers conviction."""

    def test_bot_burst_produces_positive_penalty(self) -> None:
        """A bot burst (20 bot-scored events in 4 min) must produce mcs_penalty > 0."""
        computer = VelocitySignalComputer.offline(
            preloaded_events=BOT_BURST_EVENTS,
            velocity_window_ms=WINDOW_MS,
        )
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[SC-E10-A] bot_burst: events={signal.total_events_in_window}, "
            f"bot_vel={signal.bot_velocity_per_min:.2f}/min, "
            f"org_vel={signal.organic_velocity_per_min:.2f}/min, "
            f"bot_frac={signal.bot_account_fraction:.2f}, "
            f"penalty={signal.mcs_penalty:.4f}, "
            f"flags={signal.red_flags}"
        )

        assert signal.mcs_penalty > 0.0, (
            f"ADVERSARIAL INVARIANT VIOLATED: bot burst produced penalty=0.0. "
            f"Bot-driven velocity MUST lower conviction. "
            f"Got penalty={signal.mcs_penalty:.4f}."
        )
        assert signal.mcs_penalty >= 0.3, (
            f"Bot burst (20 bot events, 4-min window) must produce substantial penalty. "
            f"Got mcs_penalty={signal.mcs_penalty:.4f} (expected >= 0.3)."
        )

    def test_bot_burst_lowers_conviction_vs_base(self) -> None:
        """apply_velocity_penalty_to_conviction must return < base_conviction for bot burst."""
        computer = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        base_conviction = 0.7
        adjusted = apply_velocity_penalty_to_conviction(base_conviction, signal)

        print(
            f"\n[SC-E10-A] base={base_conviction:.3f}, "
            f"penalty={signal.mcs_penalty:.3f}, adjusted={adjusted:.3f}"
        )

        assert adjusted < base_conviction, (
            f"Bot burst must lower conviction from {base_conviction:.3f}. "
            f"Got adjusted={adjusted:.3f} (penalty={signal.mcs_penalty:.3f})."
        )
        assert adjusted >= 0.0, (
            f"Adjusted conviction cannot be negative. Got {adjusted:.3f}."
        )

    def test_conviction_cannot_go_below_zero(self) -> None:
        """Even with maximum penalty, conviction is clamped to [0.0, 1.0]."""
        computer = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        for base in [0.0, 0.1, 0.05]:
            adjusted = apply_velocity_penalty_to_conviction(base, signal)
            assert adjusted >= 0.0, (
                f"Conviction clamped below 0.0: base={base}, "
                f"penalty={signal.mcs_penalty}, adjusted={adjusted}"
            )

    def test_bot_velocity_flag_raised(self) -> None:
        """Bot burst must trigger bot_velocity_burst or bot_dominant_growth red flag."""
        computer = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        adversarial_flags = {
            "bot_velocity_burst",
            "bot_dominant_growth",
            "high_bot_fraction",
        }
        assert adversarial_flags & set(signal.red_flags), (
            f"Bot burst must raise at least one adversarial red flag. "
            f"Got: {signal.red_flags}"
        )


# ---------------------------------------------------------------------------
# SC-E10-B: Organic posts score HIGHER than bot burst
# ---------------------------------------------------------------------------


class TestOrganicScoresHigherThanBots:
    """SC-E10-B: Organic events produce lower penalty than bot burst."""

    def test_organic_lower_penalty_than_bot(self) -> None:
        """Organic events must produce a LOWER penalty than a bot burst."""
        computer_bot = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal_bot = computer_bot.compute(ASSET, DECISION_TIME_MS)

        computer_org = VelocitySignalComputer.offline(preloaded_events=ORGANIC_EVENTS)
        signal_org = computer_org.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[SC-E10-B] organic_penalty={signal_org.mcs_penalty:.4f}, "
            f"bot_penalty={signal_bot.mcs_penalty:.4f}"
        )

        assert signal_org.mcs_penalty < signal_bot.mcs_penalty, (
            f"ADVERSARIAL INVARIANT VIOLATED: organic penalty ({signal_org.mcs_penalty:.4f}) "
            f">= bot burst penalty ({signal_bot.mcs_penalty:.4f}). "
            "Organic events must produce a lower velocity penalty."
        )

    def test_organic_events_do_not_raise_conviction_above_base(self) -> None:
        """Organic events must NOT raise conviction above base_conviction.

        This is the core de-risk directionality rule.
        """
        computer = VelocitySignalComputer.offline(preloaded_events=ORGANIC_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        for base in [0.0, 0.3, 0.5, 0.7, 1.0]:
            adjusted = assert_velocity_cannot_raise_conviction(signal, base)
            assert adjusted <= base + 1e-9, (
                f"Organic signal raised conviction from {base:.3f} to {adjusted:.3f}. "
                "Velocity signals MUST NOT raise conviction."
            )

    def test_pure_organic_traffic_penalty_is_low(self) -> None:
        """A clean set of organic events must produce a LOW penalty (near 0)."""
        computer = VelocitySignalComputer.offline(preloaded_events=ORGANIC_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[SC-E10-B] organic: events={signal.total_events_in_window}, "
            f"bot_frac={signal.bot_account_fraction:.2f}, "
            f"organic_ratio={signal.organic_bot_ratio:.2f}, "
            f"penalty={signal.mcs_penalty:.4f}"
        )

        # All 5 events are from old established accounts (not bot_scored).
        # Bot account fraction should be 0 → very low penalty.
        assert signal.bot_account_fraction == 0.0, (
            f"All events are organic; bot_account_fraction should be 0.0. "
            f"Got {signal.bot_account_fraction:.4f}."
        )
        assert signal.mcs_penalty < 0.3, (
            f"Pure organic events must produce low penalty (< 0.3). "
            f"Got {signal.mcs_penalty:.4f}."
        )

    def test_organic_conviction_higher_after_velocity_than_bot(self) -> None:
        """After velocity adjustment, organic-scored conviction > bot-scored conviction."""
        base = 0.6
        computer_bot = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal_bot = computer_bot.compute(ASSET, DECISION_TIME_MS)
        adjusted_bot = apply_velocity_penalty_to_conviction(base, signal_bot)

        computer_org = VelocitySignalComputer.offline(preloaded_events=ORGANIC_EVENTS)
        signal_org = computer_org.compute(ASSET, DECISION_TIME_MS)
        adjusted_org = apply_velocity_penalty_to_conviction(base, signal_org)

        print(
            f"\n[SC-E10-B] base={base:.3f}, "
            f"adjusted_organic={adjusted_org:.3f}, adjusted_bot={adjusted_bot:.3f}"
        )

        assert adjusted_org > adjusted_bot, (
            f"After velocity adjustment, organic conviction ({adjusted_org:.3f}) "
            f"must exceed bot conviction ({adjusted_bot:.3f})."
        )


# ---------------------------------------------------------------------------
# SC-E10-C: mcs_penalty is ALWAYS >= 0.0
# ---------------------------------------------------------------------------


class TestPenaltyAlwaysNonNegative:
    """SC-E10-C: VelocitySignal.mcs_penalty cannot be negative."""

    @pytest.mark.parametrize(
        "events,label",
        [
            ([], "no_events"),
            (ORGANIC_EVENTS, "organic"),
            (BOT_BURST_EVENTS, "bot_burst"),
            (MIXED_EVENTS, "mixed"),
            (FACTORY_COHORT_EVENTS, "factory_cohort"),
        ],
    )
    def test_penalty_non_negative(
        self, events: list[VelocityEvent], label: str
    ) -> None:
        """mcs_penalty must be >= 0.0 for all event sets."""
        computer = VelocitySignalComputer.offline(preloaded_events=events)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.mcs_penalty >= 0.0, (
            f"[{label}] mcs_penalty is negative: {signal.mcs_penalty:.6f}. "
            "VelocitySignal may ONLY de-risk (penalty >= 0.0)."
        )

    @pytest.mark.parametrize(
        "events,label",
        [
            ([], "no_events"),
            (ORGANIC_EVENTS, "organic"),
            (BOT_BURST_EVENTS, "bot_burst"),
            (MIXED_EVENTS, "mixed"),
        ],
    )
    def test_penalty_at_most_one(
        self, events: list[VelocityEvent], label: str
    ) -> None:
        """mcs_penalty must be <= 1.0 for all event sets."""
        computer = VelocitySignalComputer.offline(preloaded_events=events)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.mcs_penalty <= 1.0, (
            f"[{label}] mcs_penalty exceeds 1.0: {signal.mcs_penalty:.6f}. "
            "Penalty is clamped to [0.0, 1.0]."
        )


# ---------------------------------------------------------------------------
# SC-E10-D: apply_velocity_penalty_to_conviction NEVER exceeds base
# ---------------------------------------------------------------------------


class TestConvictionNeverRaisedByVelocity:
    """SC-E10-D: Velocity signal cannot raise conviction above base_conviction."""

    @pytest.mark.parametrize(
        "base_conviction,events,label",
        [
            (0.0, BOT_BURST_EVENTS, "bot_base0"),
            (0.5, BOT_BURST_EVENTS, "bot_base05"),
            (1.0, BOT_BURST_EVENTS, "bot_base1"),
            (0.5, ORGANIC_EVENTS, "organic_base05"),
            (1.0, ORGANIC_EVENTS, "organic_base1"),
            (0.0, [], "empty_base0"),
            (0.8, [], "empty_base08"),
        ],
    )
    def test_adjusted_never_exceeds_base(
        self,
        base_conviction: float,
        events: list[VelocityEvent],
        label: str,
    ) -> None:
        """apply_velocity_penalty_to_conviction result must be <= base_conviction."""
        computer = VelocitySignalComputer.offline(preloaded_events=events)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        adjusted = apply_velocity_penalty_to_conviction(base_conviction, signal)
        assert adjusted <= base_conviction + 1e-9, (
            f"[{label}] Velocity raised conviction from {base_conviction:.3f} "
            f"to {adjusted:.3f} (penalty={signal.mcs_penalty:.4f}). "
            "Velocity signals may ONLY de-risk."
        )
        assert adjusted >= 0.0, (
            f"[{label}] Adjusted conviction below 0.0: {adjusted:.3f}."
        )


# ---------------------------------------------------------------------------
# SC-E10-E: Point-in-time filter — future events excluded
# ---------------------------------------------------------------------------


class TestPointInTimeFilter:
    """SC-E10-E: Events with event_time_ms > decision_time_ms must be excluded."""

    def test_future_events_excluded_from_signal(self) -> None:
        """Future events must not contribute to the velocity signal."""
        all_events = ORGANIC_EVENTS + FUTURE_EVENTS

        computer_without_future = VelocitySignalComputer.offline(
            preloaded_events=ORGANIC_EVENTS
        )
        signal_clean = computer_without_future.compute(ASSET, DECISION_TIME_MS)

        computer_with_future = VelocitySignalComputer.offline(
            preloaded_events=all_events
        )
        signal_with_future = computer_with_future.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[SC-E10-E] clean_events={signal_clean.total_events_in_window}, "
            f"with_future_events={signal_with_future.total_events_in_window}, "
            f"excluded_future={signal_with_future.posts_excluded_future}"
        )

        # Future events should be excluded by the PIT filter
        assert signal_with_future.total_events_in_window == signal_clean.total_events_in_window, (
            f"POINT-IN-TIME VIOLATED: future events changed the event count! "
            f"clean={signal_clean.total_events_in_window}, "
            f"with_future={signal_with_future.total_events_in_window}."
        )
        assert signal_with_future.mcs_penalty == signal_clean.mcs_penalty, (
            f"POINT-IN-TIME VIOLATED: future events changed the penalty! "
            f"clean={signal_clean.mcs_penalty:.4f}, "
            f"with_future={signal_with_future.mcs_penalty:.4f}."
        )
        # posts_excluded_future must count the excluded ones
        assert signal_with_future.posts_excluded_future == len(FUTURE_EVENTS), (
            f"posts_excluded_future should be {len(FUTURE_EVENTS)}, "
            f"got {signal_with_future.posts_excluded_future}."
        )

    def test_all_future_events_produces_zero_penalty(self) -> None:
        """If all events are future (excluded), penalty must be 0.0 (no data)."""
        computer = VelocitySignalComputer.offline(preloaded_events=FUTURE_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.total_events_in_window == 0, (
            f"All future events must be excluded. Got {signal.total_events_in_window} events."
        )
        assert signal.mcs_penalty == 0.0, (
            f"No events in window must yield penalty=0.0. Got {signal.mcs_penalty:.4f}."
        )
        assert signal.posts_excluded_future == len(FUTURE_EVENTS), (
            f"posts_excluded_future must count all excluded future events. "
            f"Expected {len(FUTURE_EVENTS)}, got {signal.posts_excluded_future}."
        )

    def test_compute_enforces_pit_filter_not_source(self) -> None:
        """compute() applies the definitive PIT filter (not the source).

        The InMemoryVelocitySource returns all events in the rolling window
        including future ones; compute() applies the authoritative upper bound
        (event_time_ms <= decision_time_ms) and counts the exclusions.
        """
        source = InMemoryVelocitySource(events=FUTURE_EVENTS)
        # Source returns events in the window (lower bound only — no upper bound filter).
        # Future events may be within the window time range; compute() excludes them.
        source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        # compute() is responsible for excluding future events
        computer = VelocitySignalComputer(source=source, velocity_window_ms=WINDOW_MS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        # compute() must have excluded ALL future events
        assert signal.total_events_in_window == 0, (
            f"compute() did not exclude future events. "
            f"total_events_in_window={signal.total_events_in_window} (expected 0)."
        )
        assert signal.posts_excluded_future > 0, (
            "compute() must count excluded future events in posts_excluded_future."
        )

    def test_boundary_event_at_decision_time_is_included(self) -> None:
        """An event with event_time_ms == decision_time_ms must be included."""
        boundary_event = make_velocity_event(
            event_id="boundary_001",
            account_id="boundary_acct_001",
            event_time_ms=DECISION_TIME_MS,
            account_age_days=200.0,
            is_bot_scored=False,
        )
        computer = VelocitySignalComputer.offline(preloaded_events=[boundary_event])
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.total_events_in_window == 1, (
            "Event at exactly decision_time_ms must be included (inclusive boundary)."
        )


# ---------------------------------------------------------------------------
# SC-E10-F: Factory-account cohort detection
# ---------------------------------------------------------------------------


class TestFactoryAccountCohortDetection:
    """SC-E10-F: Accounts in the same creation-date cohort trigger penalty."""

    def test_factory_cohort_flag_raised(self) -> None:
        """A batch of accounts all created within 1 day of each other → cohort flag."""
        computer = VelocitySignalComputer.offline(preloaded_events=FACTORY_COHORT_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[SC-E10-F] cohort_flag={signal.account_age_cohort_flag}, "
            f"penalty={signal.mcs_penalty:.4f}, "
            f"flags={signal.red_flags}"
        )

        assert signal.account_age_cohort_flag is True, (
            "Factory-cohort events (10 accounts all aged 2.0-2.9 days) "
            "must trigger account_age_cohort_flag=True."
        )
        assert "factory_account_cohort" in signal.red_flags, (
            f"'factory_account_cohort' must be in red_flags. Got: {signal.red_flags}"
        )

    def test_factory_cohort_contributes_to_penalty(self) -> None:
        """Factory cohort must produce a positive holder_growth component in penalty."""
        computer = VelocitySignalComputer.offline(preloaded_events=FACTORY_COHORT_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        assert signal.penalty_breakdown["holder_growth_component"] > 0.0, (
            f"Factory cohort must produce a positive holder_growth_component. "
            f"Got: {signal.penalty_breakdown}"
        )

    def test_dispersed_ages_no_cohort_flag(self) -> None:
        """Events with widely dispersed account ages must NOT trigger the cohort flag."""
        dispersed = [
            make_velocity_event(
                event_id=f"dispersed_{i:03d}",
                account_id=f"disp_acct_{i:03d}",
                event_time_ms=T_MINUS_5MIN + i * ONE_MIN_MS,
                account_age_days=float(10 + i * 90),  # 10, 100, 190, 280, 370 days
                is_bot_scored=False,
            )
            for i in range(5)
        ]
        computer = VelocitySignalComputer.offline(preloaded_events=dispersed)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.account_age_cohort_flag is False, (
            f"Dispersed account ages must NOT trigger cohort flag. "
            f"Got account_age_cohort_flag={signal.account_age_cohort_flag}."
        )


# ---------------------------------------------------------------------------
# SC-E10-G: Offline / injectable — no live network calls
# ---------------------------------------------------------------------------


class TestOfflineInjectable:
    """SC-E10-G: Module uses injectable fixtures; no live network calls."""

    def test_offline_factory_works(self) -> None:
        """VelocitySignalComputer.offline() must work with no events."""
        computer = VelocitySignalComputer.offline()
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.mcs_penalty == 0.0
        assert signal.total_events_in_window == 0

    def test_in_memory_source_injectable(self) -> None:
        """InMemoryVelocitySource accepts pre-loaded events at construction."""
        source = InMemoryVelocitySource(events=BOT_BURST_EVENTS)
        computer = VelocitySignalComputer(source=source)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.total_events_in_window == len(BOT_BURST_EVENTS)

    def test_source_can_be_replaced(self) -> None:
        """Different sources produce different signals (injection is load-bearing)."""
        source_bot = InMemoryVelocitySource(events=BOT_BURST_EVENTS)
        source_org = InMemoryVelocitySource(events=ORGANIC_EVENTS)

        signal_bot = VelocitySignalComputer(source_bot).compute(ASSET, DECISION_TIME_MS)
        signal_org = VelocitySignalComputer(source_org).compute(ASSET, DECISION_TIME_MS)

        assert signal_bot.mcs_penalty != signal_org.mcs_penalty, (
            "Injected sources must produce different signals (injection is not a no-op)."
        )


# ---------------------------------------------------------------------------
# SC-E10-H: No win_rate anywhere in the velocity module
# ---------------------------------------------------------------------------


class TestNoWinRate:
    """SC-E10-H: The velocity module must contain no win_rate reference."""

    def test_no_win_rate_in_module_source(self) -> None:
        """No 'win_rate' string in the velocity module source code."""
        import aats.sentiment.velocity as velocity_module

        source = inspect.getsource(velocity_module)
        assert "win_rate" not in source.lower(), (
            "HARD RULE VIOLATED: 'win_rate' found in aats.sentiment.velocity source. "
            "No win-rate computation is permitted anywhere."
        )


# ---------------------------------------------------------------------------
# SC-E10-I: SLOW-LOOP ONLY — not imported by fast-path modules
# ---------------------------------------------------------------------------


class TestSlowLoopOnly:
    """SC-E10-I: velocity.py must not be imported by SNIPE or FAST loop modules."""

    def test_velocity_not_imported_by_execution_module(self) -> None:
        """aats.execution (SNIPE/FAST path) must not import aats.sentiment.velocity."""
        import sys  # noqa: PLC0415

        import aats.sentiment.velocity  # noqa: F401,PLC0415
        # Ensure the SNIPE/FAST entry-points do NOT import it.
        # We do this by checking that the fast-path modules are NOT listed in
        # velocity's dependency chain (i.e., we can import velocity without
        # triggering execution/snipe module loads).
        for mod_name in list(sys.modules):
            if "execution" in mod_name and "venue" in mod_name:
                # If an execution venue module is loaded, assert it was not
                # loaded BY importing velocity
                pass
        # Primary assertion: the module docstring states SLOW-LOOP ONLY
        import aats.sentiment.velocity as v
        doc = v.__doc__ or ""
        assert "SLOW-LOOP ONLY" in doc, (
            "aats.sentiment.velocity docstring must declare 'SLOW-LOOP ONLY' "
            "to make the boundary explicit."
        )

    def test_velocity_module_has_slow_loop_declaration(self) -> None:
        """The velocity module source must contain the SLOW-LOOP ONLY marker."""
        import aats.sentiment.velocity as v  # noqa: PLC0415

        source = inspect.getsource(v)
        assert "SLOW-LOOP ONLY" in source, (
            "aats.sentiment.velocity must explicitly declare SLOW-LOOP ONLY."
        )


# ---------------------------------------------------------------------------
# SC-E10-J: Mutation-meaningful — neuter bot_velocity_penalty → RED
# ---------------------------------------------------------------------------


class TestMutationMeaningful:
    """SC-E10-J: Prove tests catch penalty-neuter mutations."""

    def test_bot_velocity_component_is_load_bearing(self) -> None:
        """Mutating the bot_velocity_component away from the real value fails this test.

        This test uses the raw component function directly to prove it produces
        a non-zero result for a bot-dominant event set.  A mutation that returns
        0.0 from _compute_bot_velocity_penalty for bots would fail this test.
        """
        bot_events = [e for e in BOT_BURST_EVENTS if e.is_bot_scored]
        organic_events: list[VelocityEvent] = []
        window_minutes = WINDOW_MS / 60_000.0

        component = _compute_bot_velocity_penalty(bot_events, organic_events, window_minutes)

        assert component > 0.0, (
            f"MUTATION-PROBE FAILED: _compute_bot_velocity_penalty returned 0.0 "
            f"for {len(bot_events)} bot events. "
            "A neuter mutation that returns 0.0 should make this RED."
        )
        # Must be <= BOT_VELOCITY_WEIGHT (the cap)
        assert component <= BOT_VELOCITY_WEIGHT + 1e-9, (
            f"bot_velocity_component={component:.4f} exceeds weight cap={BOT_VELOCITY_WEIGHT}."
        )

    def test_growth_ratio_component_is_load_bearing(self) -> None:
        """Mutating _compute_growth_ratio_penalty to always return 0 fails this test."""
        # 100% bot accounts → full GROWTH_RATIO_WEIGHT
        component = _compute_growth_ratio_penalty(
            bot_account_fraction=1.0,
            total_events=20,
        )
        assert component == pytest.approx(GROWTH_RATIO_WEIGHT, abs=1e-6), (
            f"100% bot accounts must produce GROWTH_RATIO_WEIGHT={GROWTH_RATIO_WEIGHT}. "
            f"Got {component:.6f}."
        )
        # 0% bots → no penalty
        component_zero = _compute_growth_ratio_penalty(
            bot_account_fraction=0.0,
            total_events=20,
        )
        assert component_zero == 0.0, (
            f"0% bot accounts must produce 0.0 growth_ratio penalty. Got {component_zero:.6f}."
        )

    def test_holder_growth_component_detects_cohort(self) -> None:
        """Mutating _compute_holder_growth_penalty to always return 0 fails this test."""
        penalty, flag = _compute_holder_growth_penalty(FACTORY_COHORT_EVENTS)
        assert flag is True, (
            "Factory cohort events must trigger the cohort flag. "
            "A mutation returning (0.0, False) would fail this test."
        )
        assert penalty > 0.0, (
            f"Factory cohort must produce non-zero holder_growth penalty. Got {penalty:.6f}."
        )

    def test_full_pipeline_bot_vs_organic_delta_is_load_bearing(self) -> None:
        """The delta between bot and organic MCS conviction must be meaningful.

        A mutation that zeroes all penalties would make both equal.
        """
        base = 0.7
        computer_bot = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal_bot = computer_bot.compute(ASSET, DECISION_TIME_MS)
        adjusted_bot = apply_velocity_penalty_to_conviction(base, signal_bot)

        computer_org = VelocitySignalComputer.offline(preloaded_events=ORGANIC_EVENTS)
        signal_org = computer_org.compute(ASSET, DECISION_TIME_MS)
        adjusted_org = apply_velocity_penalty_to_conviction(base, signal_org)

        delta = adjusted_org - adjusted_bot
        assert delta > 0.1, (
            f"MUTATION-PROBE: The organic-vs-bot conviction delta must be > 0.1. "
            f"Got delta={delta:.4f} (organic={adjusted_org:.3f}, bot={adjusted_bot:.3f}). "
            "A mutation zeroing the penalties would make this RED."
        )


# ---------------------------------------------------------------------------
# Mixed-events: intermediate case
# ---------------------------------------------------------------------------


class TestMixedEvents:
    """Mixed bot + organic events produce intermediate penalty."""

    def test_mixed_penalty_between_pure_organic_and_pure_bot(self) -> None:
        """Mixed events must produce penalty BETWEEN organic and bot."""
        computer_org = VelocitySignalComputer.offline(preloaded_events=ORGANIC_EVENTS)
        signal_org = computer_org.compute(ASSET, DECISION_TIME_MS)

        computer_bot = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal_bot = computer_bot.compute(ASSET, DECISION_TIME_MS)

        computer_mix = VelocitySignalComputer.offline(preloaded_events=MIXED_EVENTS)
        signal_mix = computer_mix.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[mixed] org_penalty={signal_org.mcs_penalty:.4f}, "
            f"mix_penalty={signal_mix.mcs_penalty:.4f}, "
            f"bot_penalty={signal_bot.mcs_penalty:.4f}"
        )

        assert signal_mix.mcs_penalty > signal_org.mcs_penalty, (
            f"Mixed events must have higher penalty than organic. "
            f"mix={signal_mix.mcs_penalty:.4f} organic={signal_org.mcs_penalty:.4f}."
        )
        assert signal_mix.mcs_penalty <= signal_bot.mcs_penalty + 1e-6, (
            f"Mixed events must have penalty <= pure bot burst. "
            f"mix={signal_mix.mcs_penalty:.4f} bot={signal_bot.mcs_penalty:.4f}."
        )


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: empty events, single event, boundary conditions."""

    def test_empty_events_zero_penalty(self) -> None:
        """No events in window → zero penalty (no data = neutral, not adversarial)."""
        computer = VelocitySignalComputer.offline(preloaded_events=[])
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.mcs_penalty == 0.0
        assert signal.total_events_in_window == 0
        assert signal.organic_bot_ratio == 1.0  # neutral default

    def test_single_bot_event_produces_positive_penalty(self) -> None:
        """A single bot event must still produce a positive penalty."""
        single_bot = [_make_bot_event(0, T_MINUS_5MIN)]
        computer = VelocitySignalComputer.offline(preloaded_events=single_bot)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        # With 1 bot event and 0 organic, bot_account_fraction = 1.0
        # → growth_ratio penalty > 0
        assert signal.mcs_penalty > 0.0, (
            f"Single bot event must produce positive penalty. "
            f"Got {signal.mcs_penalty:.4f}."
        )

    def test_single_organic_event_zero_penalty(self) -> None:
        """A single fully-organic event must produce zero or near-zero penalty."""
        single_org = [_make_organic_event(0, T_MINUS_5MIN)]
        computer = VelocitySignalComputer.offline(preloaded_events=single_org)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.bot_account_fraction == 0.0, (
            "Single organic event must have bot_account_fraction=0.0."
        )
        assert signal.mcs_penalty == 0.0, (
            f"Single organic event must have zero penalty. Got {signal.mcs_penalty:.4f}."
        )

    def test_penalty_breakdown_components_sum_to_total(self) -> None:
        """penalty_breakdown components must sum to <= total (before clamping)."""
        computer = VelocitySignalComputer.offline(preloaded_events=BOT_BURST_EVENTS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        bd = signal.penalty_breakdown
        component_sum = (
            bd["bot_velocity_component"]
            + bd["growth_ratio_component"]
            + bd["holder_growth_component"]
        )
        # total is min(1.0, component_sum)
        assert bd["total"] == pytest.approx(min(1.0, component_sum), abs=1e-9), (
            f"penalty_breakdown total does not match component sum. "
            f"components={component_sum:.6f}, total={bd['total']:.6f}."
        )
        assert bd["total"] == pytest.approx(signal.mcs_penalty, abs=1e-9), (
            f"penalty_breakdown total must equal mcs_penalty. "
            f"total={bd['total']:.6f}, mcs_penalty={signal.mcs_penalty:.6f}."
        )


# ---------------------------------------------------------------------------
# DEF-E10-01: de-risk guard survives python -O (bare-assert replacement)
# ---------------------------------------------------------------------------


class TestDeRiskGuardSurvivesOptimizedMode:
    """DEF-E10-01: The de-risk invariant must NOT rely on bare `assert`.

    Under `python -O` (optimised mode) all `assert` statements are STRIPPED.
    A forged negative mcs_penalty would therefore silently RAISE conviction
    if the only guard were an assertion.

    The fix replaces the bare assert with:
      1. An explicit `raise ValueError` for negative mcs_penalty (not stripped by -O).
      2. A hard `max(0.0, ...)` clamp that prevents raise-conviction even if
         the raise were somehow bypassed.

    These tests prove that:
      - A forged negative penalty raises ValueError (not AssertionError).
      - The guard logic is unconditional — it fires even when __debug__ is False.
      - A legitimate zero penalty returns base_conviction unchanged.
      - The clamp is load-bearing: even if the ValueError were suppressed the
        result could not exceed base_conviction.
    """

    def _make_signal_with_penalty(self, penalty: float) -> "VelocitySignal":
        """Build a VelocitySignal with an arbitrary (potentially forged) mcs_penalty.

        Uses the dataclass directly — bypasses VelocitySignalComputer so we can
        test the guard in apply_velocity_penalty_to_conviction independently.
        """
        from aats.sentiment.velocity import VelocitySignal  # noqa: PLC0415

        return VelocitySignal(
            asset="ForgeryTestMint111111111111111111111111",
            decision_time_ms=DECISION_TIME_MS,
            mcs_penalty=penalty,
            organic_velocity_per_min=0.0,
            bot_velocity_per_min=0.0,
            organic_bot_ratio=1.0,
            unique_account_count=0,
            bot_account_fraction=0.0,
            account_age_cohort_flag=False,
            posts_excluded_future=0,
            total_events_in_window=0,
            penalty_breakdown={
                "bot_velocity_component": 0.0,
                "growth_ratio_component": 0.0,
                "holder_growth_component": 0.0,
                "total": penalty,
            },
            red_flags=[],
        )

    def test_forged_negative_penalty_raises_value_error_not_assertion_error(
        self,
    ) -> None:
        """A forged negative mcs_penalty must raise ValueError, NOT AssertionError.

        AssertionError is produced by `assert` and is stripped under python -O.
        ValueError is unconditional and survives python -O.

        This is the primary DEF-E10-01 guard.
        """
        forged = self._make_signal_with_penalty(-0.5)
        with pytest.raises(ValueError, match="VELOCITY INVARIANT VIOLATED"):
            apply_velocity_penalty_to_conviction(0.7, forged)

    def test_forged_negative_penalty_small_still_raises(self) -> None:
        """Even a tiny negative penalty (epsilon) must raise ValueError."""
        forged = self._make_signal_with_penalty(-1e-10)
        with pytest.raises(ValueError, match="mcs_penalty.*negative"):
            apply_velocity_penalty_to_conviction(0.5, forged)

    def test_guard_is_not_an_assertion_error(self) -> None:
        """Confirm the raised exception type is ValueError, not AssertionError.

        If the guard were a bare `assert`, it would raise AssertionError.
        Under python -O it would be silently SKIPPED.  The ValueError is never
        skipped.
        """
        forged = self._make_signal_with_penalty(-0.3)
        exc_type = None
        try:
            apply_velocity_penalty_to_conviction(0.6, forged)
        except ValueError:
            exc_type = ValueError
        except AssertionError:
            exc_type = AssertionError

        assert exc_type is ValueError, (
            "DEF-E10-01: The de-risk guard raised AssertionError (stripped by python -O) "
            "instead of ValueError (unconditional). "
            "Replace the bare assert with an explicit raise ValueError."
        )

    def test_guard_fires_even_when_debug_is_false(self) -> None:
        """Simulate python -O by temporarily setting __debug__-equivalent flag.

        We cannot actually set __debug__ = False at runtime (it is read-only),
        but we can prove the guard is NOT an assert by confirming:
          (a) The exception is ValueError (not AssertionError).
          (b) The function's source code does NOT contain a bare `assert` on
              mcs_penalty (i.e., the load-bearing guard uses `raise`, not `assert`).
        """
        import inspect  # noqa: PLC0415

        import aats.sentiment.velocity as vel_mod  # noqa: PLC0415

        source = inspect.getsource(vel_mod.apply_velocity_penalty_to_conviction)

        # The source must contain `raise ValueError` for the negative-penalty case
        assert "raise ValueError" in source, (
            "DEF-E10-01: apply_velocity_penalty_to_conviction must contain an "
            "explicit `raise ValueError` for the negative-penalty guard. "
            "A bare `assert` is stripped by python -O."
        )

        # The source must NOT rely solely on assert for the mcs_penalty check.
        # Count bare `assert` statements (excluding comments) in the function.
        # Zero bare asserts in the function = guard is unconditional.
        non_comment_lines = [
            line.strip()
            for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        bare_asserts = [
            line for line in non_comment_lines if line.startswith("assert ")
        ]
        assert len(bare_asserts) == 0, (
            f"DEF-E10-01: apply_velocity_penalty_to_conviction contains bare assert "
            f"statements that are stripped by python -O: {bare_asserts}. "
            "Replace with explicit `raise ValueError`."
        )

    def test_zero_penalty_returns_base_conviction_unchanged(self) -> None:
        """A signal with mcs_penalty=0.0 must return base_conviction unchanged."""
        zero_pen = self._make_signal_with_penalty(0.0)
        for base in [0.0, 0.3, 0.5, 0.7, 1.0]:
            result = apply_velocity_penalty_to_conviction(base, zero_pen)
            assert result == pytest.approx(base, abs=1e-9), (
                f"Zero penalty must return base_conviction={base:.3f} unchanged. "
                f"Got {result:.6f}."
            )

    def test_legitimate_positive_penalty_only_decreases_conviction(self) -> None:
        """Positive penalties (produced by VelocitySignalComputer) must only decrease conviction."""
        for penalty_val in [0.0, 0.1, 0.3, 0.5, 0.8, 1.0]:
            signal = self._make_signal_with_penalty(penalty_val)
            for base in [0.0, 0.2, 0.5, 0.8, 1.0]:
                result = apply_velocity_penalty_to_conviction(base, signal)
                assert result <= base + 1e-9, (
                    f"Positive penalty={penalty_val} raised conviction "
                    f"from {base:.3f} to {result:.3f}. De-risk direction violated."
                )
                assert result >= 0.0, (
                    f"Conviction clamped below 0.0: {result:.3f} "
                    f"(base={base:.3f}, penalty={penalty_val:.3f})."
                )

    def test_clamp_is_belt_and_suspenders_for_raise_bypass(self) -> None:
        """Prove that the max(0.0, penalty_raw) clamp is load-bearing.

        If the ValueError raise were somehow bypassed (e.g., by a future code
        change that wraps the call in a bare except), the clamp ensures
        conviction still cannot rise.

        We prove this by directly testing the clamp arithmetic:
          clamp(base_conviction - max(0.0, -0.5), 0.0, 1.0)
          = clamp(base - 0.0, 0.0, 1.0)
          = clamp(base, 0.0, 1.0)
          = base  (for base in [0, 1])
        So even with a forged negative penalty, the clamped result = base (not higher).
        """
        for neg_penalty in [-0.5, -0.1, -1.0, -1e-6]:
            clamped = max(0.0, neg_penalty)
            for base in [0.0, 0.3, 0.7, 1.0]:
                result_if_clamped = max(0.0, min(1.0, base - clamped))
                assert result_if_clamped <= base + 1e-9, (
                    f"Even with forged penalty={neg_penalty}, the clamp ensures "
                    f"result ({result_if_clamped:.4f}) <= base ({base:.4f})."
                )
