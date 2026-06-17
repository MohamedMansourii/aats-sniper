"""Shared fixtures + offline fakes for the T-360 Telegram ALERT channel tests.

ALL external services are INJECTED offline fakes:
  * FakeFeedSource   — an async iterator over a fixed list of SnipeEvent dicts
                       (stands in for the SSE /api/feed or Redis ops.feed tail).
  * FakeBreakerSignal — a mutable breaker snapshot source (stands in for a poll
                       of /api/state + /api/metrics).
  * OfflineTelegramClient (from aats.telegram.client) — captures sends, no token.

No real bot token is used; the config carries a placeholder so `enabled` is
False and the service runs entirely against the offline client.
"""

from __future__ import annotations

import pytest

from aats.telegram.client import OfflineTelegramClient
from aats.telegram.command_config import TelegramCommandConfig
from aats.telegram.config import TelegramAlertConfig
from aats.telegram.control_plane_client import OfflineControlPlaneClient
from aats.telegram.service import BreakerSnapshot

# A fixed operator user-ID used across the T-361 command tests.  This is a TEST
# value only (not a real account); it is the single configured operator.
OPERATOR_USER_ID = 424242
NON_OPERATOR_USER_ID = 999999

# ---------------------------------------------------------------------------
# Offline FeedSource — async iterator over a fixed list (no network)
# ---------------------------------------------------------------------------


class FakeFeedSource:
    """Deterministic async-iterable of SnipeEvent dicts (offline /api/feed)."""

    def __init__(self, events: list[dict]) -> None:
        self._events = list(events)

    def __aiter__(self) -> FakeFeedSource:
        self._idx = 0
        return self

    async def __anext__(self) -> dict:
        if self._idx >= len(self._events):
            raise StopAsyncIteration
        evt = self._events[self._idx]
        self._idx += 1
        return evt


# ---------------------------------------------------------------------------
# Offline BreakerSignal — mutable snapshot source (offline /api/state poll)
# ---------------------------------------------------------------------------


class FakeBreakerSignal:
    """Mutable breaker snapshot source for rising-edge tests."""

    def __init__(self, snapshot: BreakerSnapshot) -> None:
        self.snapshot = snapshot
        self.raise_on_read = False

    def read(self) -> BreakerSnapshot:
        if self.raise_on_read:
            raise ConnectionError("simulated breaker-signal source down")
        return self.snapshot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_client() -> OfflineTelegramClient:
    return OfflineTelegramClient()


@pytest.fixture
def alert_config() -> TelegramAlertConfig:
    # Placeholder values only — NEVER a real token (offline test posture).
    return TelegramAlertConfig(
        bot_token="<alertmanager-bot-token-or-vault-ref>",
        chat_id="<your-chat-id>",
        dry_run_enabled=True,
        enabled=False,
    )


# ---------------------------------------------------------------------------
# T-361 — command bot fixtures (offline control-plane client, operator config)
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_control_plane() -> OfflineControlPlaneClient:
    return OfflineControlPlaneClient()


@pytest.fixture
def command_config() -> TelegramCommandConfig:
    # Placeholder secret refs only — NEVER a real token (offline test posture).
    # The operator allowlist contains exactly the single test operator ID.
    return TelegramCommandConfig(
        operator_user_ids=frozenset({OPERATOR_USER_ID}),
        bot_token_vault_ref="<vault-ref: secret/aats/telegram-bot-token>",
        operator_api_token="<vault-ref: secret/aats/operator-api-token>",
        control_plane_url="http://localhost:8787",
        dry_run_enabled=True,
        enabled=False,
    )


def make_update(text: str, *, user_id: int | None = OPERATOR_USER_ID, chat_id: int = -100777) -> dict:
    """Build a minimal Telegram-update-shaped dict for the command handler.

    `user_id=None` simulates an update with no identifiable sender (must be
    rejected, fail-closed).
    """
    frm: dict | None = {"id": user_id, "is_bot": False} if user_id is not None else None
    msg: dict = {"text": text, "chat": {"id": chat_id, "type": "private"}}
    if frm is not None:
        msg["from"] = frm
    return {"update_id": 1, "message": msg}


# ---------------------------------------------------------------------------
# Event builders (valid SnipeEvent dicts per api-contracts.md §6)
# ---------------------------------------------------------------------------


def make_fill_event(
    *,
    event_id: str = "evt-fill-1",
    mint: str = "MintFillAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    token: str = "DOGE2",
    slot: int = 1000,
    sol_in_lamports: int | None = 50_000_000,
    realized_pnl_net_lamports: int | None = None,
    pnl_pct: float | None = None,
) -> dict:
    evt: dict = {
        "id": event_id,
        "event_time": {"slot": slot, "wall_clock_ms": 1_700_000_000_000},
        "observation_slot": slot,
        "confirmation_slot": slot + 1,
        "detection_transport": "geyser",
        "ts": 1_700_000_000_000,
        "slot": slot,
        "token": token,
        "mint": mint,
        "source": "pump.fun",
        "gate_passed": True,
        "gate_reasons": ["passed"],
        "red_flags": [],
        "model_p": 0.72,
        "model_uncertainty": 0.10,
        "action": "sniped",
        "veto_source": "null",
        "slot_delay": 6,
        "smart_wallets": 0,
        "tip_contention_bucket": "low",
        "cost_gate": {"expected_edge_bps": 220, "total_cost_bps": 130, "passed": True},
        "pnl_pct": pnl_pct,
    }
    if sol_in_lamports is not None:
        evt["sol_in_lamports"] = sol_in_lamports
    if realized_pnl_net_lamports is not None:
        evt["realized_pnl_net_lamports"] = realized_pnl_net_lamports
    return evt


def make_rug_avoided_event(
    *,
    event_id: str = "evt-rug-1",
    mint: str = "MintRugBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    token: str = "SCAMX",
    slot: int = 2000,
    red_flags: list[str] | None = None,
    veto_source: str = "gate",
    action: str = "vetoed",
) -> dict:
    return {
        "id": event_id,
        "event_time": {"slot": slot, "wall_clock_ms": 1_700_000_500_000},
        "observation_slot": slot,
        "confirmation_slot": slot + 1,
        "detection_transport": "geyser",
        "ts": 1_700_000_500_000,
        "slot": slot,
        "token": token,
        "mint": mint,
        "source": "raydium_v4",
        "gate_passed": False,
        "gate_reasons": ["freeze_authority", "lp_unlocked"],
        "red_flags": red_flags if red_flags is not None else ["freeze_authority", "high_tax"],
        "model_p": None,
        "model_uncertainty": None,
        "action": action,
        "veto_source": veto_source,
        "slot_delay": 9,
        "smart_wallets": 0,
        "tip_contention_bucket": "medium",
        "cost_gate": {"expected_edge_bps": 40, "total_cost_bps": 260, "passed": False},
        "pnl_pct": None,
    }


def make_skipped_noise_event(*, event_id: str = "evt-skip-noise", slot: int = 3000) -> dict:
    """A routine 'skipped' with no red flags and no veto — must NOT alert."""
    return {
        "id": event_id,
        "event_time": {"slot": slot, "wall_clock_ms": 1_700_001_000_000},
        "observation_slot": slot,
        "confirmation_slot": slot + 1,
        "detection_transport": "geyser",
        "ts": 1_700_001_000_000,
        "slot": slot,
        "token": "MEH",
        "mint": "MintMehCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        "source": "pump.fun",
        "gate_passed": True,
        "gate_reasons": ["passed"],
        "red_flags": [],
        "model_p": 0.20,
        "model_uncertainty": 0.30,
        "action": "skipped",
        "veto_source": "null",
        "slot_delay": 5,
        "smart_wallets": 0,
        "tip_contention_bucket": "low",
        "cost_gate": None,
        "pnl_pct": None,
    }
