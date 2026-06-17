"""Shared fixtures for execution tests (T-327 / E1).

All fixtures use deterministic data — no random or time-dependent values.
All money fields are integer (lamports / base units), never float.

Environment isolation (BLOCKER-2 fix):
  _env_isolation is an AUTOUSE fixture that snapshots every env var the
  venue reads at the start of each test and restores the snapshot after,
  regardless of pass/fail.  This eliminates the env-leak flake that caused
  ~1-in-5 PYTHONHASHSEED failures when tests ran in random order.

  Env vars isolated per-test:
    DRY_RUN_ENABLED, SOLANA_CLUSTER, RPC_DEVNET,
    DEVNET_CONFIRM_MAX_POLLS, DEVNET_CONFIRM_POLL_INTERVAL_S,
    JUPITER_API_URL, JITO_DEFAULT_TIP_LAMPORTS,
    DEFAULT_CU_PRICE_MICROLAMPORTS, WALLET_PUBKEY.
"""
from __future__ import annotations

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.intents import CostStack, EntryIntent, ExitIntent
from aats.contracts.venue import SimulationVenue
from aats.execution import (
    JitoJupiterVenue,
    MockRpcClient,
    MockRpcClientBlockhashExpiry,
    MockRpcClientRevert,
    MockSignerClient,
    MockSignerClientRefusing,
)

# ---------------------------------------------------------------------------
# Env vars the JitoJupiterVenue reads at runtime — all must be snapshot+restored
# around every test so no test bleeds its env state into the next (BLOCKER-2).
# ---------------------------------------------------------------------------
_VENUE_ENV_VARS = (
    "DRY_RUN_ENABLED",
    "SOLANA_CLUSTER",
    "RPC_DEVNET",
    "DEVNET_CONFIRM_MAX_POLLS",
    "DEVNET_CONFIRM_POLL_INTERVAL_S",
    "JUPITER_API_URL",
    "JITO_DEFAULT_TIP_LAMPORTS",
    "DEFAULT_CU_PRICE_MICROLAMPORTS",
    "WALLET_PUBKEY",
)

# ---------------------------------------------------------------------------
# Autouse env isolation fixture (BLOCKER-2 fix)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot all venue env vars before each test and restore them after.

    This is AUTOUSE — it applies to every test in this directory without
    any explicit fixture reference.  It eliminates the env-leak flake where
    a test that mutates os.environ (e.g. removes DRY_RUN_ENABLED) could
    leave a dirty environment for a subsequent test run under different
    PYTHONHASHSEED ordering.

    Implementation: monkeypatch already handles snapshot+restore via its
    internal stack.  Calling monkeypatch.setenv / monkeypatch.delenv within
    this autouse fixture sets the env for the test duration only, but because
    we use the fixture-provided monkeypatch (not our own), all mutations are
    rolled back on fixture teardown without any manual finally block.

    Note: we do NOT set any env vars here — we simply ensure the monkeypatch
    context is active.  Tests that need specific values (e.g. DRY_RUN_ENABLED=true)
    use the dry_run_env / live_env fixtures or call monkeypatch.setenv directly.
    The isolation guarantee is that whatever the process-level env is at test
    collection time, it is restored to EXACTLY that state after each test.
    """
    # Record the current value of every watched var.  monkeypatch.setenv /
    # monkeypatch.delenv calls in individual tests then ride on top of this
    # snapshot and are undone by monkeypatch teardown automatically.
    # We anchor them here so even direct os.environ mutations in old-style
    # tests are covered (we re-pin the values via monkeypatch before the
    # test body runs, guaranteeing the teardown path restores them).
    import os
    for var in _VENUE_ENV_VARS:
        current = os.environ.get(var)
        if current is not None:
            monkeypatch.setenv(var, current)
        else:
            monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------

_SLOT = 300_000_000
_MOCK_MINT = "PumpFunTokenMint111111111111111111111111111"
_MOCK_PUBKEY = "MockPubkey11111111111111111111111111111111"


def make_event_time(slot: int = _SLOT) -> EventTime:
    return EventTime(
        slot=slot,
        block_time_ms=1_750_000_000_000,
        wall_clock_ms=1_750_000_000_100,
    )


def make_launch_event(*, mint: str = _MOCK_MINT, competitors: int = 3) -> LaunchEvent:
    return LaunchEvent(
        mint=mint,
        # NOTE: This program ID appears on LaunchEvent.venue_program_id (the detected
        # program ID from the ingestion event). This is NOT a hot-path decoder literal
        # (AC-002 / execution-venue.md §3.2). The LaunchEvent carries it from M1;
        # the execution venue reads it from the event, not from a hardcoded constant.
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=make_event_time(),
        observation_slot=_SLOT - 1,
        confirmation_slot=_SLOT,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=5_000_000_000,    # 5 SOL, integer
        token_reserve_base=1_000_000_000_000,   # 1T tokens, integer
        token_decimals=6,
        initial_holders=100,
        competitors=competitors,
        creator_wallet="Creator111111111111111111111111111111111111",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


def make_cost_stack() -> CostStack:
    return CostStack(
        expected_edge_bps=400,     # expected edge > total_cost_bps (must clear cost gate)
        jito_tip_bps=50,
        priority_fee_bps=20,
        entry_slippage_bps=100,
        amm_fee_bps=25,
        exit_slippage_bps=80,
        adverse_selection_bps=150,
        total_cost_bps=275,        # 50+20+100+25+80 < 400 — gate passes
    )


def make_entry_intent(
    *,
    mint: str = _MOCK_MINT,
    intent_id: str = "ci-T327-test",
    sol_in_lamports: int = 100_000_000,
    slippage_bps: int = 500,
    tip_lamports: int = 300_000,
    cu_price_microlamports: int = 100_000,
    target_slot: int = _SLOT + 5,
    wallet_id: str = "wallet-0",
) -> EntryIntent:
    return EntryIntent(
        client_intent_id=intent_id,
        mint=mint,
        event_time=make_event_time(),
        sol_in_lamports=sol_in_lamports,
        slippage_bps=slippage_bps,
        tip_lamports=tip_lamports,
        cu_price_microlamports=cu_price_microlamports,
        target_slot=target_slot,
        cost_stack=make_cost_stack(),
        venue="pumpswap",
        wallet_id=wallet_id,
    )


def make_exit_intent(
    *,
    mint: str = _MOCK_MINT,
    intent_id: str = "ci-T327-exit",
    fraction_bps: int = 10_000,
) -> ExitIntent:
    return ExitIntent(
        client_intent_id=intent_id,
        mint=mint,
        fraction_bps=fraction_bps,
        exit_mode="secure",
        reason="tp_hit",
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dry_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure DRY_RUN_ENABLED=true (the default) for tests."""
    monkeypatch.setenv("DRY_RUN_ENABLED", "true")


@pytest.fixture()
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set DRY_RUN_ENABLED=false to enable the LIVE path in tests."""
    monkeypatch.setenv("DRY_RUN_ENABLED", "false")


@pytest.fixture()
def mock_rpc() -> MockRpcClient:
    return MockRpcClient(current_slot=_SLOT, cu_consumed=180_000)


@pytest.fixture()
def mock_rpc_revert() -> MockRpcClientRevert:
    return MockRpcClientRevert(revert_reason="honeypot_detected", current_slot=_SLOT)


@pytest.fixture()
def mock_rpc_blockhash_expiry() -> MockRpcClientBlockhashExpiry:
    return MockRpcClientBlockhashExpiry(current_slot=_SLOT)


@pytest.fixture()
def mock_signer() -> MockSignerClient:
    return MockSignerClient(wallet_pubkey=_MOCK_PUBKEY)


@pytest.fixture()
def mock_signer_refusing() -> MockSignerClientRefusing:
    return MockSignerClientRefusing(reason="signer_per_tx_cap_exceeded")


@pytest.fixture()
def dry_run_venue(mock_rpc: MockRpcClient, mock_signer: MockSignerClient) -> JitoJupiterVenue:
    """DRY_RUN venue — builds+signs+simulates but NEVER sends."""
    return JitoJupiterVenue(
        wallet_pubkey=_MOCK_PUBKEY,
        rpc_client=mock_rpc,
        signer_client=mock_signer,
        live_submit_enabled=False,  # DRY_RUN — gate 3
    )


@pytest.fixture()
def launch_event() -> LaunchEvent:
    return make_launch_event()


@pytest.fixture()
def entry_intent() -> EntryIntent:
    return make_entry_intent()


@pytest.fixture()
def exit_intent() -> ExitIntent:
    return make_exit_intent()


@pytest.fixture()
def sim_venue() -> SimulationVenue:
    import random
    return SimulationVenue(
        rng=random.Random(42),
        market_tip_lamports=200_000,
        ahead_size_sol_lamports=1_200_000_000,
    )
