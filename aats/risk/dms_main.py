"""T-322 — `aats-dms` service ENTRYPOINT (the separate-failure-domain process).

This is the `python -m aats.risk.dms_main` entrypoint the `aats-dms` compose unit
runs (docker-compose.yml `aats-dms`; infrastructure.md §8).  It:

  1. Reads config from the environment — T_DMS_SECONDS (default 60, ENV-CONFIGURABLE
     per OQ-006), DRY_RUN_ENABLED (default true — real capital DISABLED by default),
     REDIS_URL, the heartbeat key.
  2. Reads the FAST-loop heartbeat from the SHARED store (Redis KV in prod) across
     the process boundary.
  3. Holds the pre-signed flattens and, on heartbeat loss > T_DMS, submits them to
     the block engine — even if aats-hotcore AND aats-signer are both dead.

It is its own failure domain: it imports NO decoders, opens NO inbound network,
and shares NO memory with the FAST loop.  It cannot be disarmed by an LLM, a
market event, or a risk signal — only a fresh heartbeat or an operator stand-down.

NOTE ON BOUNDARIES: this entrypoint WIRES the pieces and runs the loop.  The
concrete Redis client, the real block-engine submit, and the upstream signing
(via aats-signer) are owned by `solana-execution-engineer` / `latency-devops-engineer`;
this module depends on their seams (HeartbeatStore, BlockEngineSubmitter) and
ships DRY-RUN-safe reference implementations so the service boots and is provable
end-to-end without real capital or a live RPC.
"""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass

from aats.contracts.venue import SubmitMode
from aats.risk.dms_service import (
    DmsServiceTelemetry,
    DmsWatchdog,
    Heartbeat,
    HeartbeatStore,
)
from aats.risk.presigned_flatten import BlockEngineSubmitter, PreSignedFlattenHolder

_DEFAULT_T_DMS_SECONDS = 60  # OQ-006 default; ALWAYS overridable by env.
_DEFAULT_HEARTBEAT_KEY = "aats:dms:heartbeat"
_DEFAULT_CHECK_INTERVAL_S = 1.0  # watchdog cadence; well under T_DMS.


# ---------------------------------------------------------------------------
# Config — read once at boot from the environment (no secrets here).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DmsServiceConfig:
    """Resolved aats-dms config.  Money/sizes are not handled here; this is the
    liveness watchdog only.  Real-capital submit is gated by `dry_run`."""

    t_dms_seconds: float
    dry_run: bool
    redis_url: str
    heartbeat_key: str
    check_interval_s: float

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> DmsServiceConfig:
        e = env if env is not None else dict(os.environ)

        # T_DMS_SECONDS — ENV-CONFIGURABLE, default 60 (OQ-006).  A non-positive or
        # unparsable value FAILS CLOSED to the conservative default (we never
        # silently disable the watchdog with a 0/garbage T_DMS).
        raw = e.get("T_DMS_SECONDS", str(_DEFAULT_T_DMS_SECONDS))
        try:
            t_dms = float(raw)
        except (TypeError, ValueError):
            t_dms = float(_DEFAULT_T_DMS_SECONDS)
        if t_dms <= 0:
            t_dms = float(_DEFAULT_T_DMS_SECONDS)

        # DRY_RUN_ENABLED — default true.  ONLY the explicit string "false"
        # disables it (fail-closed: anything else, including absent/garbage, keeps
        # real capital DISABLED).
        dry_run = e.get("DRY_RUN_ENABLED", "true").strip().lower() != "false"

        return DmsServiceConfig(
            t_dms_seconds=t_dms,
            dry_run=dry_run,
            redis_url=e.get("REDIS_URL", "redis://redis:6379"),
            heartbeat_key=e.get("DMS_HEARTBEAT_KEY", _DEFAULT_HEARTBEAT_KEY),
            check_interval_s=_DEFAULT_CHECK_INTERVAL_S,
        )


# ---------------------------------------------------------------------------
# Reference DRY-RUN submitter — boots the service without real capital / RPC.
# solana-execution-engineer supplies the real BlockEngineSubmitter for LIVE.
# ---------------------------------------------------------------------------
class DryRunBlockEngineSubmitter:
    """A submitter that NEVER transmits (submit_mode=DRY_RUN).

    There is no code path here that reaches the block engine — the held signed
    bytes are accepted and counted but not sent (FR-039; real capital DISABLED by
    default).  Swapped for the real submitter only at LIVE (DRY_RUN_ENABLED=false
    + CEO auth, gated upstream)."""

    def __init__(self) -> None:
        self.submitted_reasons: list[str] = []

    @property
    def submit_mode(self) -> SubmitMode:
        return SubmitMode.DRY_RUN

    def submit_signed(self, signed_tx_b64: str, reason: str) -> bool:
        # Record-but-do-not-transmit.  Returns False (not submitted to network).
        self.submitted_reasons.append(reason)
        return False


# ---------------------------------------------------------------------------
# Reference heartbeat store backed by Redis (read-only on the DMS side).
# ---------------------------------------------------------------------------
class RedisHeartbeatStore:
    """Reads the FAST-loop heartbeat from a Redis key across the process boundary.

    The DMS side READS only.  The FAST loop (aats-hotcore) writes the key.  An
    unreachable Redis returns None -> the watchdog treats it as 'not alive'
    (fail-closed).  The heartbeat is stored as 'epoch_ms:seq'.
    """

    def __init__(self, redis_client: object, key: str) -> None:
        self._redis = redis_client
        self._key = key

    def read(self) -> Heartbeat | None:
        try:
            raw = self._redis.get(self._key)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — unreachable store is a liveness failure
            return None
        if raw is None:
            return None
        try:
            text = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
            epoch_ms_str, seq_str = text.split(":", 1)
            return Heartbeat(epoch_ms=int(epoch_ms_str), seq=int(seq_str))
        except (ValueError, AttributeError):
            # A malformed heartbeat is NOT proof of life — fail-closed.
            return None

    @staticmethod
    def serialize(hb: Heartbeat) -> str:
        """The wire form the FAST-loop writer uses (kept here so both agree)."""
        return f"{hb.epoch_ms}:{hb.seq}"


# ---------------------------------------------------------------------------
# The runnable service.
# ---------------------------------------------------------------------------
class DmsService:
    """The runnable aats-dms watchdog service.

    Constructed from a resolved config + the (injected) seams.  `run_forever()`
    ticks the watchdog every `check_interval_s` until stopped.  The watchdog is
    the same `DmsWatchdog` proven by the test suite — this class only owns the
    loop, signal handling, and graceful shutdown.
    """

    def __init__(
        self,
        config: DmsServiceConfig,
        store: HeartbeatStore,
        holder: PreSignedFlattenHolder,
        *,
        telemetry: DmsServiceTelemetry | None = None,
    ) -> None:
        self._config = config
        self._holder = holder
        self._watchdog = DmsWatchdog(
            holder,  # PreSignedFlattenHolder implements emergency_flatten
            store,
            t_dms_seconds=config.t_dms_seconds,
            telemetry=telemetry,
        )
        self._stop = threading.Event()

    @property
    def watchdog(self) -> DmsWatchdog:
        return self._watchdog

    def request_stop(self, *_args: object) -> None:
        self._stop.set()

    def tick(self) -> bool:
        """One watchdog tick (exposed for tests / a single-step run)."""
        return self._watchdog.check()

    def run_forever(self) -> None:
        """Run the watchdog loop until stop is requested.  Never disarms on its own."""
        while not self._stop.is_set():
            self._watchdog.check()
            self._stop.wait(self._config.check_interval_s)


def _build_default_service(config: DmsServiceConfig) -> DmsService:
    """Wire the DRY-RUN-safe default service (no real capital, no live RPC needed).

    A real deployment injects a Redis-backed `RedisHeartbeatStore` and (only at
    LIVE) a real `BlockEngineSubmitter`.  The DRY-RUN default lets the compose
    unit boot, expose /health + heartbeat-age telemetry, and prove the mechanism
    without touching the network or real capital."""
    # Connect to Redis if the client lib is available; otherwise the watchdog
    # still runs and fails CLOSED (no heartbeat => ages out) — never fails OPEN.
    store: HeartbeatStore
    try:
        import redis  # type: ignore[import-not-found]

        client = redis.Redis.from_url(config.redis_url)
        store = RedisHeartbeatStore(client, config.heartbeat_key)
    except Exception:  # noqa: BLE001 — no redis => fail-closed in-memory empty store
        from aats.risk.dms_service import InMemoryHeartbeatStore

        store = InMemoryHeartbeatStore()  # empty => no heartbeat => fail-closed

    submitter: BlockEngineSubmitter = DryRunBlockEngineSubmitter()
    holder = PreSignedFlattenHolder(submitter)
    return DmsService(config, store, holder)


def main() -> None:  # pragma: no cover - process entrypoint
    config = DmsServiceConfig.from_env()
    service = _build_default_service(config)

    # Graceful shutdown on SIGTERM/SIGINT (compose stop).  A stand-down requires
    # an operator token; a process signal only stops the loop, it does NOT disarm
    # the watchdog's policy.
    signal.signal(signal.SIGTERM, service.request_stop)
    signal.signal(signal.SIGINT, service.request_stop)

    mode = "DRY-RUN (real capital disabled)" if config.dry_run else "LIVE"
    # structlog/Prometheus wiring is the telemetry layer's job; a single stderr
    # line at boot is fine and carries NO secret.
    print(  # noqa: T201 - boot banner only
        f"[aats-dms] watchdog up: T_DMS={config.t_dms_seconds:.0f}s mode={mode} "
        f"heartbeat_key={config.heartbeat_key}",
        flush=True,
    )
    service.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
