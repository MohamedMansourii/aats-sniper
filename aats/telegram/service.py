"""T-360 — TelegramAlertService: consume the control-plane feed, emit alerts.

This is the orchestration loop.  It is OUTBOUND ONLY and READ-ONLY against the
control plane: it consumes the `/api/feed` SnipeEvent stream (api-contracts.md
§6) via an injectable `FeedSource`, polls an injectable `BreakerSignal` for the
ARMED -> TRIPPED transition, formats alerts (see `alerts.py`), and pushes them
through an injectable `TelegramClient`.  It sends NO command and exposes NO
control surface — by construction it cannot increase risk.

INJECTION (every external service is a seam with a deterministic offline fake):
  * FeedSource     — async iterator of SnipeEvent dicts.  Prod: an SSE reader of
                     `/api/feed` or a Redis `ops.feed` tail.  Tests: an in-memory
                     list-backed fake.  NO Redis/RPC/Telegram reachable here.
  * BreakerSignal  — a callable returning the current breaker (state, pnl, limit,
                     tripped_at, dry_run).  Prod: a poll of `/api/state` +
                     `/api/metrics`.  Tests: a mutable fake.
  * TelegramClient — see client.py (OfflineTelegramClient in tests).

IDEMPOTENCY: the feed may redeliver and the breaker may be polled repeatedly.
Each alert carries a `dedupe_key`; a bounded LRU of seen keys suppresses
duplicates so one underlying event alerts exactly once (breaker trip alerts once
per trip timestamp).

ERROR DISCIPLINE: a single bad event or a transient send failure NEVER crashes
the loop.  Bad events are logged (without secrets) and skipped; send failures are
logged and the alert is dropped (alerts are best-effort, not at-least-once — a
missed alert must never wedge the loop that is watching for the NEXT one).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from aats.telegram.alerts import (
    AlertMessage,
    classify_event,
    format_breaker_trip,
    format_fill,
    format_rug_avoided,
)
from aats.telegram.client import TelegramClient
from aats.telegram.config import TelegramAlertConfig

log = logging.getLogger(__name__)

# How many recent dedupe keys to remember (bounded; old keys evicted FIFO).
_DEDUPE_CAPACITY = 4096


@runtime_checkable
class FeedSource(Protocol):
    """Async-iterable source of SnipeEvent dicts (api-contracts.md §6).

    Production impls wrap the SSE `/api/feed` stream or the Redis `ops.feed`
    tail.  The service only needs `__aiter__`/`__anext__`, so any async iterator
    of dicts satisfies the seam.  Iteration ends when the source is exhausted
    (tests) or the connection closes (prod, which then reconnects upstream).
    """

    def __aiter__(self) -> Any: ...
    async def __anext__(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BreakerSnapshot:
    """Point-in-time breaker reading used to detect the ARMED->TRIPPED edge."""

    tripped: bool
    daily_net_pnl_lamports: int
    daily_loss_limit_sol: str
    tripped_at_utc: str | None
    dry_run_enabled: bool


@runtime_checkable
class BreakerSignal(Protocol):
    """Returns the current breaker snapshot (a poll of /api/state + /api/metrics).

    The service detects the rising edge (was-not-tripped -> is-tripped) and
    alerts once.  Polling is monotonic from the service's view; the snapshot is
    point-in-time and carries only what the alert needs.
    """

    def read(self) -> BreakerSnapshot: ...


class _DedupeSet:
    """A bounded FIFO set of seen dedupe keys (idempotency for redelivery)."""

    def __init__(self, capacity: int = _DEDUPE_CAPACITY) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seen_before(self, key: str) -> bool:
        """Return True if `key` was already processed; otherwise record + False."""
        if key in self._seen:
            # Refresh recency so hot keys are not evicted under churn.
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


class TelegramAlertService:
    """Consume the control-plane feed + breaker signal; emit operator alerts.

    Construct with injected seams; call `run` to process a feed source to
    exhaustion (tests / one connection lifetime) or `process_event` /
    `poll_breaker` directly for fine-grained control.  Nothing here is hot-path:
    it is a formatting + outbound-I/O loop, well clear of the SNIPE/FAST budget.
    """

    def __init__(
        self,
        *,
        config: TelegramAlertConfig,
        client: TelegramClient,
        breaker_signal: BreakerSignal | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._breaker_signal = breaker_signal
        self._dedupe = _DedupeSet()
        # Last-known breaker tripped flag for rising-edge detection.
        self._breaker_was_tripped = False
        # Bounded record of recently-sent alert categories (telemetry / tests).
        self._sent_log: deque[str] = deque(maxlen=256)

    # ------------------------------------------------------------------
    # Event path (FILL / RUG-AVOIDED)
    # ------------------------------------------------------------------

    async def process_event(self, event: dict[str, Any]) -> AlertMessage | None:
        """Classify + format + send an alert for one SnipeEvent dict.

        Returns the AlertMessage that was sent, or None if the event did not
        warrant an alert (or was a duplicate, or failed to format/send).  Never
        raises on a malformed event — it is logged (no secrets) and skipped.
        """
        try:
            category = classify_event(event)
        except Exception as exc:  # noqa: BLE001 — never crash the loop on bad input
            log.warning("telegram-alert: failed to classify event: %s", type(exc).__name__)
            return None
        if category is None:
            return None

        try:
            alert = (
                format_fill(event)
                if category.value == "FILL"
                else format_rug_avoided(event)  # RUG_AVOIDED
            )
        except Exception as exc:  # noqa: BLE001 — bad/partial event, skip safely
            log.warning(
                "telegram-alert: failed to format %s event: %s",
                category.value,
                type(exc).__name__,
            )
            return None

        if self._dedupe.seen_before(alert.dedupe_key):
            return None

        sent = await self._send(alert)
        return alert if sent else None

    # ------------------------------------------------------------------
    # Breaker path (BREAKER-TRIP), rising-edge triggered
    # ------------------------------------------------------------------

    async def poll_breaker(self) -> AlertMessage | None:
        """Read the breaker signal; alert ONCE on the ARMED->TRIPPED rising edge.

        Returns the AlertMessage that was sent, or None when there is no new
        trip.  A repeated TRIPPED reading does not re-alert (dedupe by
        tripped_at_utc + edge latch).  When the breaker resets (operator-only,
        elsewhere), the latch clears so a future trip alerts again.
        """
        if self._breaker_signal is None:
            return None
        try:
            snap = self._breaker_signal.read()
        except Exception as exc:  # noqa: BLE001 — signal source down; skip this poll
            log.warning("telegram-alert: breaker signal read failed: %s", type(exc).__name__)
            return None

        if not snap.tripped:
            # Clear the latch so the next genuine trip alerts again.
            self._breaker_was_tripped = False
            return None

        # snap.tripped is True here.
        if self._breaker_was_tripped:
            return None  # already alerted for the current tripped episode
        self._breaker_was_tripped = True

        try:
            alert = format_breaker_trip(
                daily_net_pnl_lamports=snap.daily_net_pnl_lamports,
                daily_loss_limit_sol=snap.daily_loss_limit_sol,
                tripped_at_utc=snap.tripped_at_utc,
                dry_run_enabled=snap.dry_run_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram-alert: failed to format breaker trip: %s", type(exc).__name__)
            return None

        if self._dedupe.seen_before(alert.dedupe_key):
            return None

        sent = await self._send(alert)
        return alert if sent else None

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(
        self,
        feed: FeedSource,
        *,
        poll_breaker_each_event: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Process `feed` to exhaustion, emitting alerts for each event.

        When `poll_breaker_each_event` is True and a breaker signal is injected,
        the breaker is polled after each event so a trip is caught promptly even
        on a quiet feed.  `stop_event` allows deterministic teardown in tests.

        This method NEVER raises on a per-event error: it logs (no secrets) and
        continues to the next event so the loop keeps watching.
        """
        # One breaker poll up front so a breaker that was already tripped at
        # startup still produces an alert (the operator restarted into a halt).
        if poll_breaker_each_event:
            await self.poll_breaker()

        async for event in feed:
            if stop_event is not None and stop_event.is_set():
                break
            await self.process_event(event)
            if poll_breaker_each_event:
                await self.poll_breaker()

    # ------------------------------------------------------------------
    # Outbound send (with offline-safe error handling)
    # ------------------------------------------------------------------

    async def _send(self, alert: AlertMessage) -> bool:
        """Send one alert; return True on success, False on a handled failure.

        A transport failure is logged (without the token) and the alert is
        dropped — alerts are best-effort and must never wedge the loop.
        """
        try:
            await self._client.send_message(self._config.chat_id, alert.text)
        except Exception as exc:  # noqa: BLE001 — best-effort delivery
            log.warning(
                "telegram-alert: send failed for %s: %s",
                alert.category.value,
                type(exc).__name__,
            )
            return False
        self._sent_log.append(alert.category.value)
        log.info("telegram-alert: sent %s alert", alert.category.value)
        return True

    @property
    def sent_categories(self) -> list[str]:
        """Ordered list of categories successfully sent (telemetry / tests)."""
        return list(self._sent_log)
