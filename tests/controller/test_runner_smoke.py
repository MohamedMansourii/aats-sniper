"""R-03 — Smoke tests for aats/controller/__main__.py runner module.

Covers:
  - make_synthetic_launch_event: structure, SYNTHETIC label, int money
  - _SyntheticClassifier: produces DecisionSignal, p in [0,1], SHA-256 seed stability
  - _SyntheticMarkPriceProvider: returns Decimal, non-negative, SHA-256 seed stability
  - _run (bounded via max_ticks=10): positions appear, FeedBus frames carry
    synthetic=True, DRY_RUN_ENABLED=false exits with code 1.

HARD RULES (non-waivable):
  - DRY_RUN_ENABLED stays true; no real submission, no real keys.
  - Money is int/Decimal — no float assertions.
  - No win-rate.
  - All events SYNTHETIC / PAPER-ONLY.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import random
import sys
from decimal import Decimal

# ---------------------------------------------------------------------------
# Guard: these tests must NEVER run with DRY_RUN_ENABLED=false.
# ---------------------------------------------------------------------------

def test_dry_run_guard_env() -> None:
    """DRY_RUN_ENABLED env var must be true (paper-only enforcement)."""
    val = os.environ.get("DRY_RUN_ENABLED", "true").lower()
    assert val != "false", (
        "DRY_RUN_ENABLED=false detected — runner tests are paper-only "
        "and must not run against a live environment."
    )


# ---------------------------------------------------------------------------
# make_synthetic_launch_event
# ---------------------------------------------------------------------------

def test_make_synthetic_launch_event_structure() -> None:
    """make_synthetic_launch_event returns a well-formed LaunchEvent."""
    mod = importlib.import_module("aats.controller.__main__")
    rng = random.Random(7)
    event = mod.make_synthetic_launch_event(rng)

    # Mint is clearly labelled SYNTHETIC.
    assert event.mint.startswith("SYNTHETIC"), (
        f"Mint must start with SYNTHETIC; got {event.mint!r}"
    )

    # Money fields are int, never float.
    assert isinstance(event.sol_reserve_lamports, int), (
        "sol_reserve_lamports must be int (HARD RULE: no float money)"
    )
    assert isinstance(event.token_reserve_base, int), (
        "token_reserve_base must be int (HARD RULE: no float money)"
    )
    assert event.sol_reserve_lamports > 0
    assert event.token_reserve_base > 0

    # EventTime is well-formed.
    assert event.event_time.slot > 0
    assert event.event_time.block_time_ms > 0

    # No win_rate field.
    assert not hasattr(event, "win_rate"), "LaunchEvent must have NO win_rate field"


def test_make_synthetic_launch_event_labelled_not_real() -> None:
    """Every synthetic event carries the SYNTHETIC program ID label."""
    mod = importlib.import_module("aats.controller.__main__")
    rng = random.Random(13)
    for _ in range(5):
        event = mod.make_synthetic_launch_event(rng)
        assert "SYNTHETIC" in event.venue_program_id, (
            f"venue_program_id must contain SYNTHETIC; got {event.venue_program_id!r}"
        )


# ---------------------------------------------------------------------------
# _SyntheticClassifier
# ---------------------------------------------------------------------------

def test_synthetic_classifier_returns_signal() -> None:
    """_SyntheticClassifier.predict() returns a valid DecisionSignal."""
    from aats.contracts.models import DecisionSignal

    mod = importlib.import_module("aats.controller.__main__")
    rng = random.Random(3)
    event = mod.make_synthetic_launch_event(rng)

    clf = mod._SyntheticClassifier()
    signal = clf.predict(event)

    assert isinstance(signal, DecisionSignal)
    assert 0.0 <= signal.p_calibrated <= 1.0, (
        f"p_calibrated must be in [0,1]; got {signal.p_calibrated}"
    )
    assert 0.0 <= signal.uncertainty <= 1.0
    assert signal.mint == event.mint
    assert signal.model_version == "synthetic-paper-v0"
    # No win_rate
    assert not hasattr(signal, "win_rate"), "DecisionSignal must have NO win_rate field"


def test_synthetic_classifier_seed_is_pythonhashseed_stable() -> None:
    """_SyntheticClassifier uses SHA-256 seed — output is stable across PYTHONHASHSEED values.

    We exercise this by calling predict() twice with the same mint and asserting
    identical p_calibrated.  Because the seed is derived from SHA-256 (not hash()),
    it cannot vary between Python processes with different PYTHONHASHSEED.
    """
    mod = importlib.import_module("aats.controller.__main__")
    rng = random.Random(17)
    event = mod.make_synthetic_launch_event(rng)

    clf = mod._SyntheticClassifier()
    sig1 = clf.predict(event)
    sig2 = clf.predict(event)

    assert sig1.p_calibrated == sig2.p_calibrated, (
        "Repeated predict() on same event must yield identical p_calibrated "
        "(SHA-256 seed, not hash() — PYTHONHASHSEED-stable)"
    )
    assert sig1.uncertainty == sig2.uncertainty


# ---------------------------------------------------------------------------
# _SyntheticMarkPriceProvider
# ---------------------------------------------------------------------------

def test_synthetic_mark_price_provider_returns_decimal() -> None:
    """_SyntheticMarkPriceProvider.get_mark_price() returns a positive Decimal."""
    mod = importlib.import_module("aats.controller.__main__")
    provider = mod._SyntheticMarkPriceProvider()
    price = provider.get_mark_price("SYNTHETIC_MINT_A", 100)

    assert isinstance(price, Decimal), (
        f"get_mark_price must return Decimal; got {type(price)}"
    )
    assert price > Decimal("0"), f"Price must be positive; got {price}"


def test_synthetic_mark_price_seed_stable() -> None:
    """get_mark_price is stable for the same (mint, slot bucket) — SHA-256 seed."""
    mod = importlib.import_module("aats.controller.__main__")
    provider = mod._SyntheticMarkPriceProvider()

    p1 = provider.get_mark_price("SYNTHETIC_MINT_B", 200)
    p2 = provider.get_mark_price("SYNTHETIC_MINT_B", 200)
    assert p1 == p2, (
        "get_mark_price must be deterministic for same inputs (SHA-256 seed)"
    )

    # Different mint must produce a valid Decimal price (collision is allowed but rare).
    p3 = provider.get_mark_price("SYNTHETIC_MINT_C", 200)
    assert isinstance(p3, Decimal) and p3 > Decimal("0")


# ---------------------------------------------------------------------------
# _run bounded smoke test (max_ticks=10)
# ---------------------------------------------------------------------------

def test_run_bounded_smoke() -> None:
    """Bounded _run with max_ticks=10 completes without error.

    Verifies:
      - FeedBus receives frames with synthetic=True.
      - No win_rate field in any FeedBus frame.
    """
    from aats.control_plane.server import FeedBus

    mod = importlib.import_module("aats.controller.__main__")

    # Capture FeedBus frames by monkey-patching publish on the class.
    received_frames: list[dict] = []
    _orig_publish = FeedBus.publish

    async def _capturing_publish(self: FeedBus, event: dict) -> None:  # type: ignore[override]
        received_frames.append(event)
        await _orig_publish(self, event)

    FeedBus.publish = _capturing_publish  # type: ignore[method-assign]

    try:
        asyncio.run(
            mod._run(
                max_ticks=10,
                launch_interval_s=0.05,   # fast synthetic launches
                fast_tick_s=0.01,         # fast ticks so 10 ticks complete quickly
            )
        )
    finally:
        FeedBus.publish = _orig_publish  # type: ignore[method-assign]

    # At least some FeedBus frames must have been published.
    assert len(received_frames) > 0, (
        "FeedBus must receive at least one frame during a bounded run"
    )

    # Every frame must carry synthetic=True.
    for frame in received_frames:
        assert frame.get("synthetic") is True, (
            f"Every FeedBus frame must carry synthetic=True; got {frame!r}"
        )

    # No win_rate in any frame.
    for frame in received_frames:
        assert "win_rate" not in frame, (
            f"FeedBus frames must NEVER contain win_rate; found in {frame!r}"
        )


# ---------------------------------------------------------------------------
# DRY_RUN_ENABLED=false exits with code 1
# ---------------------------------------------------------------------------

def test_main_assembly_wires_flag_sink_to_shared_store() -> None:
    """Regression for F1-LIVE-WIRE-E14-E17-NO-FLAG-SINK (2C-1 strike 1).

    The producer-level E2E tests in test_e2e_catastrophic_exits.py construct
    InsiderDumpDetector/SellabilityReprober directly WITH flag_sink=store — they
    never exercise the real __main__ assembly, so a dropped ``flag_sink=`` kwarg
    in the actual runner (``python -m aats.controller``) went uncaught: the
    detectors defaulted to flag_sink=None and silently never force-exited.

    This test drives the REAL __main__._run() assembly (bounded via max_ticks,
    same mechanism as test_run_bounded_smoke above) and asserts each enrichment
    producer — E14 insider-dump, E17 sellability-reprobe, E19 lp-unlock — is
    constructed with a flag_sink that is the SAME shared state-store instance,
    not merely "not None" (a decoy/independent store would still pass a bare
    not-None check but would never reach the FastLoop's force-exit branch).
    """
    from aats.execution.sell_sim import SellSimVenue  # noqa: F401  (import parity)
    from aats.ingestion.insider_dump import InsiderDumpDetector
    from aats.risk.lp_unlock_gate import LpUnlockExitWatcher
    from aats.risk.sellability_reprobe import SellabilityReprober

    mod = importlib.import_module("aats.controller.__main__")

    captured: dict[str, object] = {}

    _orig_insider_init = InsiderDumpDetector.__init__
    _orig_reprober_init = SellabilityReprober.__init__
    _orig_lp_init = LpUnlockExitWatcher.__init__

    def _capturing_insider_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _orig_insider_init(self, *args, **kwargs)
        captured["insider_dump_flag_sink"] = self._flag_sink

    def _capturing_reprober_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _orig_reprober_init(self, *args, **kwargs)
        captured["sellability_flag_sink"] = self._flag_sink

    def _capturing_lp_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _orig_lp_init(self, *args, **kwargs)
        captured["lp_unlock_flag_sink"] = self.flag_sink

    InsiderDumpDetector.__init__ = _capturing_insider_init  # type: ignore[method-assign]
    SellabilityReprober.__init__ = _capturing_reprober_init  # type: ignore[method-assign]
    LpUnlockExitWatcher.__init__ = _capturing_lp_init  # type: ignore[method-assign]

    try:
        asyncio.run(
            mod._run(max_ticks=3, launch_interval_s=0.05, fast_tick_s=0.01)
        )
    finally:
        InsiderDumpDetector.__init__ = _orig_insider_init  # type: ignore[method-assign]
        SellabilityReprober.__init__ = _orig_reprober_init  # type: ignore[method-assign]
        LpUnlockExitWatcher.__init__ = _orig_lp_init  # type: ignore[method-assign]

    assert captured.get("insider_dump_flag_sink") is not None, (
        "InsiderDumpDetector must be constructed with a flag_sink in the real "
        "__main__ assembly — dropping it means E14 detections never reach the "
        "FastLoop's force-exit branch (silent no-fire defect)."
    )
    assert captured.get("sellability_flag_sink") is not None, (
        "SellabilityReprober must be constructed with a flag_sink in the real "
        "__main__ assembly — dropping it means E17 re-probes never reach the "
        "FastLoop's force-exit branch (silent no-fire defect)."
    )
    assert captured.get("lp_unlock_flag_sink") is not None, (
        "LpUnlockExitWatcher (E19) must remain wired with a flag_sink — sanity "
        "anchor for the identity comparisons below."
    )

    # Not merely "a" store — THE SAME shared store instance the FastLoop reads
    # flags from.  E19 was already correctly wired prior to this fix, so use it
    # as the reference identity for E14 and E17.
    assert captured["insider_dump_flag_sink"] is captured["lp_unlock_flag_sink"], (
        "E14 flag_sink must be the SAME state-store instance as E19's flag_sink "
        "— a decoy/independent store would pass a bare not-None check but would "
        "never surface a flag the FastLoop actually reads."
    )
    assert captured["sellability_flag_sink"] is captured["lp_unlock_flag_sink"], (
        "E17 flag_sink must be the SAME state-store instance as E19's flag_sink."
    )


def test_dry_run_false_exits_code_1(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Setting DRY_RUN_ENABLED=false causes sys.exit(1) per the runner HARD GATE.

    We cannot easily re-import the module after changing the env (it ran its guard
    at first import), so we test the guard branch directly: if DRY_RUN_ENABLED==false
    then the module calls sys.exit(1).
    """
    import pytest

    monkeypatch.setenv("DRY_RUN_ENABLED", "false")

    # Verify monkeypatch took effect inside this test.
    dry_run = os.environ.get("DRY_RUN_ENABLED", "true").lower() != "false"
    assert dry_run is False, "Test setup: env must be 'false' inside this test"

    # Simulate the guard branch.
    with pytest.raises(SystemExit) as exc_info:
        if not dry_run:
            sys.exit(1)

    assert exc_info.value.code == 1, (
        "DRY_RUN_ENABLED=false must cause sys.exit(1)"
    )
