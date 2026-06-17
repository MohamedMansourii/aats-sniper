"""T-322 — PROOF-BY-EXECUTION for the aats-dms separate-failure-domain service.

These tests prove the backing-the-breaker dead-man's switch SERVICE (NOT the
T-321 in-process primitive):

  1. Heartbeat absence > T_DMS, read across the SHARED store (Redis stand-in),
     triggers the flatten — with the FAST-loop 'process' fully dead.
  2. A live heartbeat (written by the separate writer) SUPPRESSES the fire.
  3. NO signal — LLM, market, or risk — can disarm it.  Only an operator
     DmsStandDownToken can, and the automated path cannot mint one.
  4. T_DMS is ENV-CONFIGURABLE (default 60; override honored; garbage fails closed).
  5. The pre-signed flatten fires even with aats-signer 'down' (held signed bytes).
  6. DRY-RUN: real capital is DISABLED by default — the submit never transmits.
  7. Fail-closed everywhere: stuck heartbeat, unreachable store, throwing submit.

Every test drives the watchdog through an INJECTED monotonic + wall clock so the
passage of time (and the death of the heartbeat) is deterministic.
"""

from __future__ import annotations

import pytest

from aats.contracts.venue import SubmitMode
from aats.risk.deadman import DmsStandDownToken, mint_dms_standdown_token
from aats.risk.dms_main import (
    DmsService,
    DmsServiceConfig,
    DryRunBlockEngineSubmitter,
    RedisHeartbeatStore,
)
from aats.risk.dms_service import (
    DmsWatchdog,
    Heartbeat,
    InMemoryHeartbeatStore,
    build_heartbeat,
)
from aats.risk.presigned_flatten import (
    PreSignedFlatten,
    PreSignedFlattenHolder,
)

MINT_A = "So1111111111111111111111111111111111111111"
MINT_B = "So2222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# Deterministic clocks shared by writer ('alive' loop) and watchdog.
# ---------------------------------------------------------------------------
class _WallClock:
    """A controllable wall-clock (epoch ms) shared by writer and watchdog."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return self.ms

    def advance_s(self, secs: float) -> None:
        self.ms += int(secs * 1000)


class _Monotonic:
    """A controllable monotonic clock for the watchdog's observed-age measure."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


# ---------------------------------------------------------------------------
# A submitter that records transmits; a 'signer down' submitter that throws.
# ---------------------------------------------------------------------------
class _RecordingLiveSubmitter:
    """Stand-in for the real block-engine submitter (LIVE)."""

    def __init__(self) -> None:
        self.transmitted: list[str] = []

    @property
    def submit_mode(self) -> SubmitMode:
        return SubmitMode.LIVE

    def submit_signed(self, signed_tx_b64: str, reason: str) -> bool:
        # Already-signed bytes go straight to the block engine (no signer call).
        self.transmitted.append(signed_tx_b64)
        return True


class _ThrowingSubmitter:
    """A submitter whose transmit raises (RPC / block-engine error)."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def submit_mode(self) -> SubmitMode:
        return SubmitMode.LIVE

    def submit_signed(self, signed_tx_b64: str, reason: str) -> bool:
        self.calls += 1
        raise RuntimeError("simulated block-engine submit failure")


def _flatten(mint: str) -> PreSignedFlatten:
    return PreSignedFlatten(mint=mint, signed_tx_b64=f"SIGNED::{mint}")


def _build_watchdog(t_dms: float = 60.0, *, live: bool = True):
    """Wire a watchdog + holder against the SHARED store and a fresh clock pair."""
    wall = _WallClock()
    mono = _Monotonic()
    store = InMemoryHeartbeatStore()
    submitter = _RecordingLiveSubmitter() if live else DryRunBlockEngineSubmitter()
    holder = PreSignedFlattenHolder(submitter)
    holder.upsert(_flatten(MINT_A))
    holder.upsert(_flatten(MINT_B))
    watchdog = DmsWatchdog(
        holder,
        store,
        t_dms_seconds=t_dms,
        wall_epoch_ms=wall,
        monotonic=mono,
    )
    return watchdog, store, holder, submitter, wall, mono


def _beat(store: InMemoryHeartbeatStore, wall: _WallClock, seq: int) -> None:
    """The SEPARATE writer ('aats-hotcore' process) proves liveness."""
    store.write(build_heartbeat(seq=seq, wall_epoch_ms=wall))


# ===========================================================================
# 1. Heartbeat absence > T_DMS triggers flatten (cross-process, loop DEAD).
# ===========================================================================
def test_heartbeat_absence_beyond_t_dms_triggers_flatten():
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0)

    # Steady state: the separate writer beats; the watchdog stays quiet.
    _beat(store, wall, seq=1)
    assert watchdog.check() is False
    assert submitter.transmitted == []

    # --- SIMULATE PROCESS DEATH: the writer stops beating entirely. ---
    # Both clocks advance past T_DMS with no new heartbeat in the shared store.
    wall.advance_s(61.0)
    mono.advance(61.0)

    fired = watchdog.check()
    assert fired is True
    # EVERY held pre-signed flatten was submitted (full book).
    assert len(submitter.transmitted) == 2
    assert set(submitter.transmitted) == {"SIGNED::" + MINT_A, "SIGNED::" + MINT_B}
    assert watchdog.is_fired() is True

    # Fires EXACTLY once and latches — a later check does not re-submit.
    wall.advance_s(120.0)
    mono.advance(120.0)
    assert watchdog.check() is False
    assert len(submitter.transmitted) == 2


def test_missing_heartbeat_from_the_start_fails_closed():
    """No heartbeat EVER written (e.g. loop never came up) ages out and fires."""
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0)
    # Never write a beat.  Within T_DMS the freshly-armed watchdog stays quiet…
    assert watchdog.check() is False
    # …but once the (seeded) liveness anchor ages past T_DMS, it fires.
    wall.advance_s(61.0)
    mono.advance(61.0)
    assert watchdog.check() is True
    assert len(submitter.transmitted) == 2


# ===========================================================================
# 2. A live heartbeat suppresses the fire.
# ===========================================================================
def test_live_heartbeat_suppresses_fire():
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0)
    # The separate writer beats every 30s — comfortably under the 60s budget.
    for seq in range(1, 11):
        wall.advance_s(30.0)
        mono.advance(30.0)
        _beat(store, wall, seq=seq)
        assert watchdog.check() is False
    assert watchdog.is_fired() is False
    assert submitter.transmitted == []


def test_stuck_heartbeat_still_ages_out():
    """A FROZEN heartbeat (same seq re-read, clock advancing) is NOT proof of life."""
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0)
    _beat(store, wall, seq=1)
    assert watchdog.check() is False
    # The writer wedged: the key stays at seq=1, epoch frozen, but time marches on.
    wall.advance_s(61.0)
    mono.advance(61.0)
    # Both the wall age (epoch frozen) AND the observed age (seq frozen) exceed
    # T_DMS — the watchdog fires despite a heartbeat being PRESENT.
    assert watchdog.check() is True
    assert len(submitter.transmitted) == 2


# ===========================================================================
# 3. NO signal can disarm it — only an operator token.
# ===========================================================================
def test_no_automated_caller_can_mint_a_standdown_token():
    """A market/LLM/risk caller cannot construct the disarm capability."""
    with pytest.raises(PermissionError):
        DmsStandDownToken("attacker")  # direct construction forbidden


def test_standdown_requires_genuine_token_not_a_duck_type():
    watchdog, *_ = _build_watchdog()

    class _FakeToken:
        operator_id = "spoof"

    with pytest.raises(PermissionError):
        watchdog.stand_down(_FakeToken())  # type: ignore[arg-type]
    # Still armed after the forged disarm attempt.
    assert watchdog.is_armed() is True


def test_market_or_llm_signal_cannot_disarm_only_operator_can():
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0)

    # No risk/LLM/market API exists on the watchdog to disarm — assert the ONLY
    # disarm path is the operator token, and it works.
    token = mint_dms_standdown_token(operator_id="ceo-1")
    watchdog.stand_down(token)
    assert watchdog.is_armed() is False

    # With the watchdog deliberately stood down by the OPERATOR, a lost heartbeat
    # does NOT fire (planned maintenance).  This is the ONLY way to suppress it.
    wall.advance_s(120.0)
    mono.advance(120.0)
    assert watchdog.check() is False
    assert submitter.transmitted == []

    # Operator re-arm restores the switch; a lost heartbeat now fires again.
    watchdog.rearm(token)
    assert watchdog.is_armed() is True
    wall.advance_s(120.0)
    mono.advance(120.0)
    assert watchdog.check() is True
    assert len(submitter.transmitted) == 2


def test_watchdog_exposes_no_risk_increasing_or_reset_by_signal_surface():
    """Structural: the watchdog's public API can only de-risk / observe.

    A market/LLM/risk caller has NO method to widen, size up, or auto-reset the
    switch.  Disarm/rearm both require the operator token.  This asserts the
    method surface, so a future edit that adds an automated disarm fails this test.
    """
    watchdog, *_ = _build_watchdog()
    public = {n for n in dir(watchdog) if not n.startswith("_")}
    # The only state-mutating methods are check (fire), stand_down, rearm (both
    # token-gated).  No 'disarm', 'reset', 'widen', 'size_up', 'override'.
    forbidden = {"disarm", "reset", "widen", "size_up", "scale_up", "override", "arm_from_llm"}
    assert public & forbidden == set()
    # stand_down / rearm exist but are token-gated (tested above).
    assert {"stand_down", "rearm", "check"} <= public


# ===========================================================================
# 4. T_DMS is ENV-CONFIGURABLE (default 60; override honored; garbage closed).
# ===========================================================================
def test_t_dms_default_is_60_when_absent():
    cfg = DmsServiceConfig.from_env(env={})
    assert cfg.t_dms_seconds == 60.0


def test_t_dms_env_override_is_honored():
    cfg = DmsServiceConfig.from_env(env={"T_DMS_SECONDS": "30"})
    assert cfg.t_dms_seconds == 30.0


def test_t_dms_garbage_or_nonpositive_fails_closed_to_default():
    for bad in ("not-a-number", "0", "-5", ""):
        cfg = DmsServiceConfig.from_env(env={"T_DMS_SECONDS": bad})
        # Never silently disable the watchdog with a 0/garbage T_DMS.
        assert cfg.t_dms_seconds == 60.0


def test_configured_t_dms_drives_the_watchdog_fire_boundary():
    """A 30s T_DMS fires at 31s; a fresh 30s watchdog does not fire at 29s."""
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=30.0)
    _beat(store, wall, seq=1)
    wall.advance_s(29.0)
    mono.advance(29.0)
    assert watchdog.check() is False  # inside the (configured) budget
    wall.advance_s(2.0)
    mono.advance(2.0)
    assert watchdog.check() is True  # 31s > 30s configured budget


# ===========================================================================
# 5. Pre-signed flatten fires even with aats-signer 'down' (held signed bytes).
# ===========================================================================
def test_fires_with_signer_down_because_flattens_are_pre_signed():
    """The DMS holds SIGNED bytes — no call to aats-signer at fire time.

    We model 'signer down' by giving the holder a submitter that NEVER calls a
    signer (it just transmits the held signed bytes).  The fire still flattens the
    whole book, proving the pre-signed design survives a dead signer
    (infrastructure.md §8)."""
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0)
    # No signer object is referenced anywhere in the holder/submitter — by
    # construction there is nothing to be 'down'.  The held bytes are pre-signed.
    _beat(store, wall, seq=1)
    wall.advance_s(61.0)
    mono.advance(61.0)
    assert watchdog.check() is True
    assert len(submitter.transmitted) == 2  # both pre-signed flattens transmitted


def test_throwing_submit_still_latches_fired_and_attempts_all_positions():
    """FAIL-CLOSED: a throwing block-engine submit still latches FIRED, and the
    holder attempts EVERY position before propagating (one bad position cannot
    suppress flattening the rest)."""
    wall = _WallClock()
    mono = _Monotonic()
    store = InMemoryHeartbeatStore()
    submitter = _ThrowingSubmitter()
    holder = PreSignedFlattenHolder(submitter)
    holder.upsert(_flatten(MINT_A))
    holder.upsert(_flatten(MINT_B))
    watchdog = DmsWatchdog(holder, store, t_dms_seconds=60.0, wall_epoch_ms=wall, monotonic=mono)
    _beat(store, wall, seq=1)
    wall.advance_s(61.0)
    mono.advance(61.0)
    with pytest.raises(RuntimeError):
        watchdog.check()  # fires; the latch is set BEFORE the throwing submit
    assert watchdog.is_fired() is True  # latched despite the throw (fail-closed)
    assert submitter.calls == 2  # attempted BOTH positions, not just the first
    # A subsequent check does NOT re-fire (latched) — no retry storm.
    submitter.calls = 0
    assert watchdog.check() is False
    assert submitter.calls == 0


# ===========================================================================
# 6. DRY-RUN: real capital DISABLED by default — submit never transmits.
# ===========================================================================
def test_dry_run_default_disables_real_capital_submit():
    watchdog, store, holder, submitter, wall, mono = _build_watchdog(t_dms=60.0, live=False)
    assert holder.submit_mode == SubmitMode.DRY_RUN  # real capital DISABLED
    _beat(store, wall, seq=1)
    wall.advance_s(61.0)
    mono.advance(61.0)
    assert watchdog.check() is True
    # The flatten was BUILT/held but the DRY-RUN submitter refused to transmit.
    assert isinstance(submitter, DryRunBlockEngineSubmitter)
    assert len(submitter.submitted_reasons) == 2  # both held, none transmitted to net


def test_service_config_dry_run_defaults_true_and_only_explicit_false_disables():
    assert DmsServiceConfig.from_env(env={}).dry_run is True
    assert DmsServiceConfig.from_env(env={"DRY_RUN_ENABLED": "true"}).dry_run is True
    assert DmsServiceConfig.from_env(env={"DRY_RUN_ENABLED": "garbage"}).dry_run is True
    assert DmsServiceConfig.from_env(env={"DRY_RUN_ENABLED": "false"}).dry_run is False
    assert DmsServiceConfig.from_env(env={"DRY_RUN_ENABLED": "FALSE"}).dry_run is False


# ===========================================================================
# 7. Money / type invariants — NEVER float.
# ===========================================================================
def test_heartbeat_rejects_float():
    with pytest.raises(TypeError):
        Heartbeat(epoch_ms=1.0, seq=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Heartbeat(epoch_ms=1, seq=1.0)  # type: ignore[arg-type]


def test_presigned_flatten_is_full_de_risk_only():
    # A partial (risk-leaving) flatten is structurally forbidden.
    with pytest.raises(ValueError):
        PreSignedFlatten(mint=MINT_A, signed_tx_b64="x", fraction_bps=5_000)
    # An empty signed tx is rejected (must be a real signed tx).
    with pytest.raises(ValueError):
        PreSignedFlatten(mint=MINT_A, signed_tx_b64="")
    # float fraction rejected.
    with pytest.raises(TypeError):
        PreSignedFlatten(mint=MINT_A, signed_tx_b64="x", fraction_bps=10_000.0)  # type: ignore[arg-type]


# ===========================================================================
# 8. The runnable service ticks the watchdog (entrypoint smoke).
# ===========================================================================
def test_service_tick_fires_after_t_dms():
    wall = _WallClock()
    mono = _Monotonic()
    store = InMemoryHeartbeatStore()
    submitter = _RecordingLiveSubmitter()
    holder = PreSignedFlattenHolder(submitter)
    holder.upsert(_flatten(MINT_A))
    config = DmsServiceConfig(
        t_dms_seconds=60.0,
        dry_run=False,
        redis_url="redis://x",
        heartbeat_key="aats:dms:heartbeat",
        check_interval_s=1.0,
    )
    # Inject the deterministic clocks into the watchdog the service owns.
    service = DmsService(config, store, holder)
    service.watchdog._wall_epoch_ms = wall  # noqa: SLF001 - test injection
    service.watchdog._monotonic = mono  # noqa: SLF001 - test injection
    service.watchdog._last_fresh_mono = mono()  # re-seed anchor to injected clock

    _beat(store, wall, seq=1)
    assert service.tick() is False
    wall.advance_s(61.0)
    mono.advance(61.0)
    assert service.tick() is True
    assert len(submitter.transmitted) == 1


def test_redis_heartbeat_store_roundtrip_and_failclosed():
    """The Redis-stand-in store parses 'epoch:seq' and fails closed on garbage."""

    class _FakeRedis:
        def __init__(self) -> None:
            self.val: bytes | None = None
            self.raise_on_get = False

        def get(self, key: str):  # noqa: ANN001
            if self.raise_on_get:
                raise ConnectionError("redis down")
            return self.val

    fake = _FakeRedis()
    store = RedisHeartbeatStore(fake, "aats:dms:heartbeat")

    hb = Heartbeat(epoch_ms=1_700_000_000_000, seq=7)
    fake.val = RedisHeartbeatStore.serialize(hb).encode()
    got = store.read()
    assert got == hb

    # Unreachable Redis -> None (fail-closed: an unreachable store is not 'alive').
    fake.raise_on_get = True
    assert store.read() is None

    # Malformed value -> None (a garbage heartbeat is not proof of life).
    fake.raise_on_get = False
    fake.val = b"not-a-heartbeat"
    assert store.read() is None
