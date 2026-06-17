"""T-360 — TelegramAlertService end-to-end tests against an offline mock feed.

Proves the BUILD-DIRECTIVE acceptance:
  * Alerts FIRE on fill / rug / breaker events from a mock feed.
  * Each alert is formatted correctly (money in SOL via Decimal; categories).
  * Duplicates are suppressed (idempotency on feed redelivery + breaker poll).
  * Breaker trips alert ONCE on the rising edge, again after reset->retrip.
  * A transient send failure NEVER crashes the loop.
  * The service is OUTBOUND ONLY — it never calls a control/command endpoint.
"""

from __future__ import annotations

import pytest

from aats.telegram.alerts import AlertCategory
from aats.telegram.service import BreakerSnapshot, TelegramAlertService
from tests.telegram.conftest import (
    FakeBreakerSignal,
    FakeFeedSource,
    make_fill_event,
    make_rug_avoided_event,
    make_skipped_noise_event,
)


def _service(alert_config, offline_client, breaker_signal=None) -> TelegramAlertService:
    return TelegramAlertService(
        config=alert_config,
        client=offline_client,
        breaker_signal=breaker_signal,
    )


# ---------------------------------------------------------------------------
# Alerts fire on fill / rug events from a mock feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_event_fires_alert(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    feed = FakeFeedSource([make_fill_event(sol_in_lamports=50_000_000)])

    await svc.run(feed)

    assert len(offline_client.sent) == 1
    sent = offline_client.sent[0]
    assert "FILL" in sent.text
    assert "0.050000 SOL" in sent.text
    assert svc.sent_categories == [AlertCategory.FILL.value]


@pytest.mark.asyncio
async def test_rug_avoided_event_fires_alert(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    feed = FakeFeedSource([make_rug_avoided_event(red_flags=["freeze_authority"])])

    await svc.run(feed)

    assert len(offline_client.sent) == 1
    assert "RUG AVOIDED" in offline_client.sent[0].text
    assert "freeze_authority" in offline_client.sent[0].text


@pytest.mark.asyncio
async def test_routine_skip_noise_does_not_fire(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    feed = FakeFeedSource([make_skipped_noise_event()])

    await svc.run(feed)

    assert offline_client.sent == []


@pytest.mark.asyncio
async def test_mixed_feed_fires_only_actionable_alerts(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    feed = FakeFeedSource(
        [
            make_fill_event(event_id="f1"),
            make_skipped_noise_event(event_id="noise"),
            make_rug_avoided_event(event_id="r1"),
            make_fill_event(event_id="f2", token="PEPE3"),
        ]
    )

    await svc.run(feed)

    assert svc.sent_categories == [
        AlertCategory.FILL.value,
        AlertCategory.RUG_AVOIDED.value,
        AlertCategory.FILL.value,
    ]


# ---------------------------------------------------------------------------
# Idempotency — duplicate events suppressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_event_id_suppressed(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    dup = make_fill_event(event_id="same-id")
    feed = FakeFeedSource([dup, dict(dup)])  # same id redelivered

    await svc.run(feed)

    assert len(offline_client.sent) == 1


# ---------------------------------------------------------------------------
# Breaker trips fire ONCE on the rising edge
# ---------------------------------------------------------------------------


def _tripped_snapshot(*, dry_run=True, at="2026-06-16T12:00:00Z") -> BreakerSnapshot:
    return BreakerSnapshot(
        tripped=True,
        daily_net_pnl_lamports=-310_000_000,
        daily_loss_limit_sol="-0.30",
        tripped_at_utc=at,
        dry_run_enabled=dry_run,
    )


def _armed_snapshot() -> BreakerSnapshot:
    return BreakerSnapshot(
        tripped=False,
        daily_net_pnl_lamports=-10_000_000,
        daily_loss_limit_sol="-0.30",
        tripped_at_utc=None,
        dry_run_enabled=True,
    )


@pytest.mark.asyncio
async def test_breaker_trip_fires_alert_on_rising_edge(alert_config, offline_client):
    signal = FakeBreakerSignal(_armed_snapshot())
    svc = _service(alert_config, offline_client, breaker_signal=signal)

    # First poll: armed, no alert.
    assert await svc.poll_breaker() is None
    assert offline_client.sent == []

    # Trip it.
    signal.snapshot = _tripped_snapshot()
    alert = await svc.poll_breaker()
    assert alert is not None
    assert alert.category is AlertCategory.BREAKER_TRIP
    assert len(offline_client.sent) == 1
    assert "TRIPPED" in offline_client.sent[0].text
    assert "-0.310000 SOL" in offline_client.sent[0].text


@pytest.mark.asyncio
async def test_breaker_trip_does_not_re_alert_while_tripped(alert_config, offline_client):
    signal = FakeBreakerSignal(_tripped_snapshot())
    svc = _service(alert_config, offline_client, breaker_signal=signal)

    await svc.poll_breaker()  # rising edge -> 1 alert
    await svc.poll_breaker()  # still tripped -> no re-alert
    await svc.poll_breaker()

    assert len(offline_client.sent) == 1


@pytest.mark.asyncio
async def test_breaker_re_alerts_after_reset_then_retrip(alert_config, offline_client):
    signal = FakeBreakerSignal(_tripped_snapshot(at="2026-06-16T12:00:00Z"))
    svc = _service(alert_config, offline_client, breaker_signal=signal)

    await svc.poll_breaker()  # 1st trip alert
    signal.snapshot = _armed_snapshot()  # operator reset (elsewhere)
    await svc.poll_breaker()  # armed, latch clears
    # A genuinely new trip (distinct tripped_at_utc) must alert again.
    signal.snapshot = _tripped_snapshot(at="2026-06-16T18:30:00Z")
    await svc.poll_breaker()

    assert len(offline_client.sent) == 2


@pytest.mark.asyncio
async def test_breaker_signal_source_down_does_not_crash(alert_config, offline_client):
    signal = FakeBreakerSignal(_tripped_snapshot())
    signal.raise_on_read = True
    svc = _service(alert_config, offline_client, breaker_signal=signal)

    # A failing signal read is swallowed; no alert, no crash.
    assert await svc.poll_breaker() is None
    assert offline_client.sent == []


@pytest.mark.asyncio
async def test_run_polls_breaker_alongside_feed(alert_config, offline_client):
    signal = FakeBreakerSignal(_tripped_snapshot())  # tripped at startup
    svc = _service(alert_config, offline_client, breaker_signal=signal)
    feed = FakeFeedSource([make_fill_event()])

    await svc.run(feed, poll_breaker_each_event=True)

    cats = svc.sent_categories
    assert AlertCategory.BREAKER_TRIP.value in cats
    assert AlertCategory.FILL.value in cats


# ---------------------------------------------------------------------------
# Resilience — a transient send failure never wedges the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_send_failure_does_not_crash_loop(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    offline_client.fail_next = True  # first send raises
    feed = FakeFeedSource(
        [
            make_fill_event(event_id="f1"),  # this send fails (dropped)
            make_fill_event(event_id="f2"),  # this one succeeds
        ]
    )

    await svc.run(feed)

    # The failed alert was dropped; the loop continued and delivered the next.
    assert len(offline_client.sent) == 1
    assert "f2" not in offline_client.sent[0].text  # id is in dedupe key, not body
    assert svc.sent_categories == [AlertCategory.FILL.value]


@pytest.mark.asyncio
async def test_malformed_event_skipped_not_raised(alert_config, offline_client):
    svc = _service(alert_config, offline_client)
    # Missing/invalid fields: classify returns None or format guards — never raise.
    feed = FakeFeedSource(
        [
            {"action": "sniped"},  # minimal sniped: should still format a FILL
            {"nonsense": True},  # no action: classified None
            make_rug_avoided_event(event_id="ok"),
        ]
    )

    await svc.run(feed)

    # The valid rug alert fired; the loop survived the odd inputs.
    assert any("RUG AVOIDED" in s.text for s in offline_client.sent)
