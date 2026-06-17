"""T-360 — alert classification + formatting tests (pure, offline).

Covers:
  * Money is formatted from integer lamports via Decimal — float REJECTED.
  * Classification: sniped->FILL, vetoed/skipped+flags->RUG_AVOIDED, noise->None.
  * No win-rate / outcome-rate field, target, or symbol anywhere in any alert.
  * Breaker-trip text states the DRY-RUN posture and never overrides it.
"""

from __future__ import annotations

import re

import pytest

from aats.telegram.alerts import (
    AlertCategory,
    classify_event,
    format_breaker_trip,
    format_fill,
    format_rug_avoided,
    format_sol_from_lamports,
)
from tests.telegram.conftest import (
    make_fill_event,
    make_rug_avoided_event,
    make_skipped_noise_event,
)

# A liberal pattern catching any win-rate-ish wording (HONESTY CLAUSE AC-037).
_WINRATE_PATTERN = re.compile(r"win[\s_-]?rate|winrate|hit[\s_-]?rate|success[\s_-]?rate", re.I)


# ---------------------------------------------------------------------------
# Money formatting — integer lamports / Decimal, NEVER float
# ---------------------------------------------------------------------------


def test_format_sol_from_lamports_uses_decimal_not_float():
    assert format_sol_from_lamports(0) == "+0.000000 SOL"
    assert format_sol_from_lamports(1_000_000_000) == "+1.000000 SOL"
    assert format_sol_from_lamports(50_000_000) == "+0.050000 SOL"
    assert format_sol_from_lamports(-300_000_000) == "-0.300000 SOL"
    # 1 lamport is exactly representable (no binary-float drift).
    assert format_sol_from_lamports(1) == "+0.000000 SOL"  # < display quantum, rounds to 0
    assert format_sol_from_lamports(1_234_567) == "+0.001235 SOL"


def test_format_sol_from_lamports_rejects_float():
    with pytest.raises(TypeError):
        format_sol_from_lamports(0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        format_sol_from_lamports(1.0)  # type: ignore[arg-type]


def test_format_sol_from_lamports_rejects_bool():
    # bool is an int subclass but never a valid lamport quantity.
    with pytest.raises(TypeError):
        format_sol_from_lamports(True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_sniped_is_fill():
    assert classify_event(make_fill_event()) is AlertCategory.FILL


def test_classify_vetoed_with_flags_is_rug_avoided():
    assert classify_event(make_rug_avoided_event()) is AlertCategory.RUG_AVOIDED


def test_classify_skipped_with_veto_is_rug_avoided():
    evt = make_rug_avoided_event(action="skipped", veto_source="cost_gate", red_flags=[])
    assert classify_event(evt) is AlertCategory.RUG_AVOIDED


def test_classify_routine_skip_noise_is_none():
    # skipped, no red flags, veto_source == "null" -> not an alert.
    assert classify_event(make_skipped_noise_event()) is None


def test_classify_unknown_action_is_none():
    assert classify_event({"action": "something_else"}) is None
    assert classify_event({}) is None


# ---------------------------------------------------------------------------
# FILL formatting
# ---------------------------------------------------------------------------


def test_format_fill_contains_size_in_sol():
    evt = make_fill_event(sol_in_lamports=50_000_000)
    msg = format_fill(evt)
    assert msg.category is AlertCategory.FILL
    assert "FILL" in msg.text
    assert "0.050000 SOL" in msg.text
    assert evt["token"] in msg.text


def test_format_fill_includes_net_pnl_when_present():
    evt = make_fill_event(realized_pnl_net_lamports=-12_345_678)
    msg = format_fill(evt)
    assert "net pnl: -0.012346 SOL" in msg.text


def test_format_fill_rejects_float_money_in_event():
    evt = make_fill_event()
    evt["sol_in_lamports"] = 0.05  # float money — must blow up at format time
    with pytest.raises(TypeError):
        format_fill(evt)


def test_format_fill_pnl_pct_is_per_event_not_winrate():
    evt = make_fill_event(pnl_pct=12.5)
    msg = format_fill(evt)
    # The per-event pnl percentage is surfaced labelled "pnl", never as a rate.
    assert "pnl: 12.50%" in msg.text
    assert not _WINRATE_PATTERN.search(msg.text)


# ---------------------------------------------------------------------------
# RUG-AVOIDED formatting
# ---------------------------------------------------------------------------


def test_format_rug_avoided_lists_red_flags():
    evt = make_rug_avoided_event(red_flags=["freeze_authority", "high_tax"])
    msg = format_rug_avoided(evt)
    assert msg.category is AlertCategory.RUG_AVOIDED
    assert "RUG AVOIDED" in msg.text
    assert "freeze_authority" in msg.text
    assert "high_tax" in msg.text
    assert "vetoed by: gate" in msg.text


def test_format_rug_avoided_uses_gate_reasons_when_no_red_flags():
    evt = make_rug_avoided_event(red_flags=[], veto_source="cost_gate")
    msg = format_rug_avoided(evt)
    # gate_reasons carries the explanation when red_flags is empty.
    assert "freeze_authority" in msg.text or "lp_unlocked" in msg.text


# ---------------------------------------------------------------------------
# BREAKER-TRIP formatting
# ---------------------------------------------------------------------------


def test_format_breaker_trip_money_and_posture():
    msg = format_breaker_trip(
        daily_net_pnl_lamports=-310_000_000,
        daily_loss_limit_sol="-0.30",
        tripped_at_utc="2026-06-16T12:00:00Z",
        dry_run_enabled=True,
    )
    assert msg.category is AlertCategory.BREAKER_TRIP
    assert "TRIPPED" in msg.text
    assert "-0.310000 SOL" in msg.text
    assert "-0.30 SOL" in msg.text
    assert "DRY-RUN" in msg.text  # posture stated, never overridden
    assert "manual review" in msg.text


def test_format_breaker_trip_rejects_float_pnl():
    with pytest.raises(TypeError):
        format_breaker_trip(
            daily_net_pnl_lamports=-0.31,  # type: ignore[arg-type]
            daily_loss_limit_sol="-0.30",
            tripped_at_utc=None,
            dry_run_enabled=True,
        )


def test_breaker_dedupe_key_is_stable_per_trip():
    a = format_breaker_trip(
        daily_net_pnl_lamports=-310_000_000,
        daily_loss_limit_sol="-0.30",
        tripped_at_utc="2026-06-16T12:00:00Z",
        dry_run_enabled=True,
    )
    b = format_breaker_trip(
        daily_net_pnl_lamports=-320_000_000,  # pnl moved, same trip
        daily_loss_limit_sol="-0.30",
        tripped_at_utc="2026-06-16T12:00:00Z",
        dry_run_enabled=True,
    )
    assert a.dedupe_key == b.dedupe_key


# ---------------------------------------------------------------------------
# HONESTY CLAUSE — no win-rate anywhere in any alert text
# ---------------------------------------------------------------------------


def test_no_winrate_in_any_alert_text():
    texts = [
        format_fill(make_fill_event(pnl_pct=42.0)).text,
        format_rug_avoided(make_rug_avoided_event()).text,
        format_breaker_trip(
            daily_net_pnl_lamports=-300_000_000,
            daily_loss_limit_sol="-0.30",
            tripped_at_utc="2026-06-16T12:00:00Z",
            dry_run_enabled=False,
        ).text,
    ]
    for t in texts:
        assert not _WINRATE_PATTERN.search(t), f"win-rate wording leaked into alert: {t!r}"
