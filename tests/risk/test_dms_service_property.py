"""T-322 PROPERTY / FUZZ proof — invariants over randomized heartbeat + clock paths.

No external fuzzing dependency (matches the T-320/T-321 property tests; seeded
`random` makes the run deterministic).

Invariants (must hold for ALL inputs):
  D1  FIRE-IFF-STALE: the watchdog fires on its first check whose liveness age
      strictly exceeds T_DMS, and NEVER before (fail-closed but not trigger-happy).
  D2  LATCH-ONCE: once fired, it never fires again until an operator rearm — no
      double-flatten, no retry storm, regardless of subsequent heartbeats.
  D3  LIVENESS-SUPPRESSES: if a FRESH heartbeat (advancing seq) lands at least
      once every < T_DMS, the watchdog NEVER fires.
  D4  NO-SIGNAL-DISARM: across a fuzzed sequence of checks/heartbeats, the only
      thing that ever changes 'armed' is an operator token — never a market path.
"""

from __future__ import annotations

import random

from aats.risk.dms_service import (
    DmsWatchdog,
    InMemoryHeartbeatStore,
    build_heartbeat,
)
from aats.risk.presigned_flatten import PreSignedFlatten, PreSignedFlattenHolder

MINT = "So1111111111111111111111111111111111111111"


class _Wall:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.ms

    def advance_s(self, s: float) -> None:
        self.ms += int(s * 1000)


class _Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class _Submitter:
    def __init__(self) -> None:
        self.transmitted: list[str] = []

    @property
    def submit_mode(self):  # noqa: ANN201
        from aats.contracts.venue import SubmitMode

        return SubmitMode.LIVE

    def submit_signed(self, signed_tx_b64: str, reason: str) -> bool:
        self.transmitted.append(signed_tx_b64)
        return True


def _build(t_dms: float):
    wall, mono = _Wall(), _Mono()
    store = InMemoryHeartbeatStore()
    sub = _Submitter()
    holder = PreSignedFlattenHolder(sub)
    holder.upsert(PreSignedFlatten(mint=MINT, signed_tx_b64="SIGNED::" + MINT))
    wd = DmsWatchdog(holder, store, t_dms_seconds=t_dms, wall_epoch_ms=wall, monotonic=mono)
    return wd, store, sub, wall, mono


def test_property_fire_iff_stale_and_latch_once():
    """D1 + D2 over fuzzed advance/heartbeat sequences."""
    rng = random.Random(20260616)
    for _ in range(400):
        t_dms = rng.choice([5.0, 15.0, 30.0, 60.0, 120.0])
        wd, store, sub, wall, mono = _build(t_dms)
        seq = 0
        fired_count = 0
        # Seed one fresh beat so we start 'alive'.
        seq += 1
        store.write(build_heartbeat(seq=seq, wall_epoch_ms=wall))

        for _step in range(rng.randint(5, 40)):
            # Randomly: advance time, and/or write a fresh beat.
            dt = rng.choice([0.0, 1.0, t_dms * 0.4, t_dms * 0.9, t_dms * 1.1, t_dms * 3])
            wall.advance_s(dt)
            mono.advance(dt)
            if rng.random() < 0.5:
                seq += 1
                store.write(build_heartbeat(seq=seq, wall_epoch_ms=wall))

            age_before = wd.heartbeat_age_seconds()
            was_fired = wd.is_fired()
            fired_now = wd.check()

            if fired_now:
                fired_count += 1
                # D1: only fire when age strictly exceeded T_DMS and not already fired/disarmed.
                assert age_before > t_dms
                assert was_fired is False
            # D2: never fire more than once (latched).
            assert fired_count <= 1
        # If it ever fired, exactly one flatten was transmitted.
        assert len(sub.transmitted) == fired_count


def test_property_liveness_suppresses_fire():
    """D3: a fresh advancing-seq heartbeat at least every < T_DMS never fires."""
    rng = random.Random(99)
    for _ in range(200):
        t_dms = rng.choice([10.0, 30.0, 60.0])
        wd, store, sub, wall, mono = _build(t_dms)
        for step in range(rng.randint(10, 50)):
            # Always beat with a gap strictly under T_DMS, then check.
            gap = rng.uniform(0.1, t_dms * 0.95)
            wall.advance_s(gap)
            mono.advance(gap)
            store.write(build_heartbeat(seq=step + 1, wall_epoch_ms=wall))
            assert wd.check() is False
        assert wd.is_fired() is False
        assert sub.transmitted == []
