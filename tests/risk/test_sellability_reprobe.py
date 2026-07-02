"""E17 — SLOW-loop delayed-honeypot / tax-flip sellability RE-PROBE.

VERIFY criteria proven here (per the task board):
  * A DEGRADED re-probe (sim-revert / zero-output / high sell-tax / account-close
    trap) sets the ``sellability_degraded`` flag AND, wired into the ExitEngine,
    triggers a SECURE force-exit.
  * A HEALTHY re-probe (shared sell-sim confirmed sellable) does NOT set the flag
    and does NOT force an exit.
  * An INCONCLUSIVE re-probe (shared sell-sim raised) FAILS CLOSED to degraded
    (refuse-by-default) — it sets the flag.
  * The SHARED sell-sim (aats.execution.sell_sim.SellSimVenue) is REUSED, not
    forked: a real ``SellSimVenue`` with the shared honeypot/normal RPC mocks drives
    the re-probe, and this test module + the re-probe module define no honeypot
    mechanics of their own (grep-proven below).
  * DRY-RUN by construction: the injected sell-sim never submits.
  * Point-in-time: ``event_slot`` is an on-chain slot (event-time); no wall-clock.
  * De-risk only: the re-probe can only ever SET the flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from aats.contracts.venue import SubmitMode
from aats.controller.state import InMemoryStateStore
from aats.execution.sell_sim import (
    MockHoneypotRpcClient,
    MockSellSimRpcClient,
    SellSimConfig,
    SellSimReason,
    SellSimResult,
    SellSimVenue,
)
from aats.execution.signer_client import MockSignerClient
from aats.risk.exit_engine import ExitEngine, ExitReason, ExitState, MevExitMode, preset
from aats.risk.sellability_reprobe import (
    DEFAULT_FLAG_TTL_SECONDS,
    SellabilityReprober,
    SellabilityReprobeReason,
)

_MINT = "OpenPositionMint111111111111111111111111111"
_SLOT = 300_000_777
_WALLET = "SellSimTestPubkey1111111111111111111111111"
_ENTRY = Decimal("100")
_BT = 1_700_000_000_000


# ---------------------------------------------------------------------------
# A configurable fake probe — lets us drive every classification path without
# reaching for the RPC.  It exposes the SAME simulate_sell(...) surface the
# SHARED SellSimVenue exposes (this is the seam we reuse, not honeypot logic).
# ---------------------------------------------------------------------------


@dataclass
class _FakeProbe:
    """Returns a canned SellSimResult, or raises, on simulate_sell."""

    result: SellSimResult | None = None
    raises: BaseException | None = None
    calls: int = 0

    def simulate_sell(self, *, mint: str, event_slot: int) -> SellSimResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        assert self.result is not None
        return self.result


def _result(reason: SellSimReason, *, sellable: bool) -> SellSimResult:
    return SellSimResult(sellable=sellable, reason=reason, mint=_MINT, event_slot=_SLOT)


def _fixed_clock():
    t = {"ms": 1_000_000}
    return lambda: t["ms"], t


# ---------------------------------------------------------------------------
# StateStore flag mechanism (mirrors insider_dump — same TTL presence semantics)
# ---------------------------------------------------------------------------


def test_state_flag_absent_reads_false():
    store = InMemoryStateStore()
    assert store.get_sellability_degraded_flag(_MINT) is False
    assert store.get_sellability_degraded_detail(_MINT) is None


def test_state_flag_set_then_get_with_payload():
    store = InMemoryStateStore()
    store.set_sellability_degraded_flag(_MINT, reason="degraded_sim_revert", event_slot=_SLOT)
    assert store.get_sellability_degraded_flag(_MINT) is True
    assert store.get_sellability_degraded_detail(_MINT) == ("degraded_sim_revert", _SLOT)


def test_state_flag_ttl_expiry():
    clock, t = _fixed_clock()
    store = InMemoryStateStore(clock=clock)
    store.set_sellability_degraded_flag(_MINT, reason="inconclusive", event_slot=1, ttl_seconds=3)
    assert store.get_sellability_degraded_flag(_MINT) is True
    t["ms"] += 3_000  # advance exactly to expiry
    assert store.get_sellability_degraded_flag(_MINT) is False
    assert store.get_sellability_degraded_detail(_MINT) is None


def test_state_flag_isolated_per_mint():
    store = InMemoryStateStore()
    store.set_sellability_degraded_flag("MINT_A", reason="degraded_other", event_slot=1)
    assert store.get_sellability_degraded_flag("MINT_A") is True
    assert store.get_sellability_degraded_flag("MINT_B") is False


def test_state_flag_rejects_empty_reason():
    store = InMemoryStateStore()
    with pytest.raises(ValueError):
        store.set_sellability_degraded_flag(_MINT, reason="", event_slot=1)


def test_state_flag_rejects_bool_event_slot():
    store = InMemoryStateStore()
    with pytest.raises(TypeError):
        store.set_sellability_degraded_flag(_MINT, reason="x", event_slot=True)  # type: ignore[arg-type]


def test_state_flag_rejects_negative_event_slot():
    store = InMemoryStateStore()
    with pytest.raises(ValueError):
        store.set_sellability_degraded_flag(_MINT, reason="x", event_slot=-1)


# ---------------------------------------------------------------------------
# Classification — refuse-by-default: HEALTHY only on a confirmed-sellable result.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sell_reason", "sellable", "expected"),
    [
        (SellSimReason.SELLABLE, True, SellabilityReprobeReason.HEALTHY),
        (SellSimReason.SIM_REVERT, False, SellabilityReprobeReason.DEGRADED_SIM_REVERT),
        (SellSimReason.ZERO_OUTPUT, False, SellabilityReprobeReason.DEGRADED_ZERO_OUTPUT),
        (SellSimReason.HIGH_SELL_TAX, False, SellabilityReprobeReason.DEGRADED_HIGH_SELL_TAX),
        (
            SellSimReason.ACCOUNT_CLOSE_TRAP,
            False,
            SellabilityReprobeReason.DEGRADED_ACCOUNT_CLOSE_TRAP,
        ),
        (SellSimReason.SIGNER_REFUSED, False, SellabilityReprobeReason.DEGRADED_OTHER),
        (SellSimReason.QUOTE_FAILED, False, SellabilityReprobeReason.DEGRADED_OTHER),
        (SellSimReason.INFRASTRUCTURE_ERROR, False, SellabilityReprobeReason.DEGRADED_OTHER),
        (SellSimReason.NOT_SIMULATED, False, SellabilityReprobeReason.DEGRADED_OTHER),
    ],
)
def test_classification_maps_every_sellsim_reason(sell_reason, sellable, expected):
    probe = _FakeProbe(result=_result(sell_reason, sellable=sellable))
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=probe, flag_sink=store)
    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)
    assert res is not None
    assert res.reason is expected
    assert res.degraded is (expected is not SellabilityReprobeReason.HEALTHY)


def test_contradictory_sellable_true_but_not_sellable_reason_is_inconclusive():
    """Defense in depth: a self-inconsistent result (sellable=True but reason not
    SELLABLE) is never trusted — it is INCONCLUSIVE (degraded, sets the flag)."""
    probe = _FakeProbe(result=_result(SellSimReason.SIM_REVERT, sellable=True))
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=probe, flag_sink=store)
    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)
    assert res is not None
    assert res.reason is SellabilityReprobeReason.INCONCLUSIVE
    assert res.degraded is True
    assert store.get_sellability_degraded_flag(_MINT) is True


# ---------------------------------------------------------------------------
# Degraded re-probe SETS the flag; healthy does NOT.
# ---------------------------------------------------------------------------


def test_degraded_reprobe_sets_flag():
    probe = _FakeProbe(result=_result(SellSimReason.SIM_REVERT, sellable=False))
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=probe, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    assert res is not None and res.degraded is True and res.flag_set is True
    assert store.get_sellability_degraded_flag(_MINT) is True
    assert store.get_sellability_degraded_detail(_MINT) == ("degraded_sim_revert", _SLOT)
    assert reprober.stats.degraded == 1 and reprober.stats.flags_set == 1


def test_healthy_reprobe_does_not_set_flag():
    probe = _FakeProbe(result=_result(SellSimReason.SELLABLE, sellable=True))
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=probe, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    assert res is not None and res.degraded is False and res.flag_set is False
    assert store.get_sellability_degraded_flag(_MINT) is False
    assert reprober.stats.healthy == 1 and reprober.stats.flags_set == 0


def test_inconclusive_reprobe_fails_closed_to_degraded():
    """REFUSE-BY-DEFAULT: the shared sell-sim RAISING is treated as DEGRADED, never
    healthy — the flag is set.  Mutation canary: swap the except branch to 'healthy'
    and this goes RED."""
    probe = _FakeProbe(raises=RuntimeError("rpc exploded mid-simulate"))
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=probe, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    assert res is not None
    assert res.reason is SellabilityReprobeReason.INCONCLUSIVE
    assert res.degraded is True and res.flag_set is True
    assert res.sell_sim_reason is None
    assert store.get_sellability_degraded_flag(_MINT) is True
    assert reprober.stats.probe_exceptions == 1


def test_not_open_position_is_skipped_no_flag():
    probe = _FakeProbe(result=_result(SellSimReason.SIM_REVERT, sellable=False))
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=probe, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=False)

    assert res is None  # nothing to protect -> no probe, no flag
    assert probe.calls == 0
    assert store.get_sellability_degraded_flag(_MINT) is False
    assert reprober.stats.not_open_skipped == 1


# ---------------------------------------------------------------------------
# End-to-end with the SHARED SellSimVenue (proves genuine reuse, DRY-RUN).
# ---------------------------------------------------------------------------


def _real_venue(rpc) -> SellSimVenue:
    return SellSimVenue(
        rpc_client=rpc,
        signer_client=MockSignerClient(wallet_pubkey=_WALLET),
        config=SellSimConfig(),
        wallet_pubkey=_WALLET,
    )


def test_shared_sellsim_honeypot_drives_degraded_flag():
    """A real SellSimVenue over the SHARED MockHoneypotRpcClient (simulate reverts)
    yields a DEGRADED re-probe and sets the flag — proving the shared mechanics are
    what decide the verdict."""
    venue = _real_venue(MockHoneypotRpcClient(current_slot=_SLOT))
    assert venue.submit_mode is SubmitMode.DRY_RUN  # DRY-RUN by construction
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=venue, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    assert res is not None and res.degraded is True
    assert res.reason is SellabilityReprobeReason.DEGRADED_SIM_REVERT
    assert store.get_sellability_degraded_flag(_MINT) is True


def test_shared_sellsim_normal_token_stays_healthy():
    """A real SellSimVenue over the SHARED MockSellSimRpcClient (1% tax, sellable)
    yields a HEALTHY re-probe — no flag."""
    venue = _real_venue(
        MockSellSimRpcClient(simulate_sell_ok=True, sell_out_lamports=9_900, current_slot=_SLOT)
    )
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=venue, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    assert res is not None and res.degraded is False
    assert res.reason is SellabilityReprobeReason.HEALTHY
    assert store.get_sellability_degraded_flag(_MINT) is False


def test_shared_sellsim_high_tax_flip_drives_degraded_flag():
    """A tax-FLIP after entry: the SHARED sell-sim reports HIGH_SELL_TAX (out far
    below quote) -> DEGRADED -> flag set."""
    # quote 10_000, sim out 5_000 -> 50% effective tax, above the 10% ceiling.
    rpc = MockSellSimRpcClient(
        simulate_sell_ok=True,
        quote_out_lamports=10_000,
        sell_out_lamports=5_000,
        current_slot=_SLOT,
    )
    venue = _real_venue(rpc)
    store = InMemoryStateStore()
    reprober = SellabilityReprober(probe=venue, flag_sink=store)

    res = reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    assert res is not None and res.degraded is True
    assert res.reason is SellabilityReprobeReason.DEGRADED_HIGH_SELL_TAX
    assert store.get_sellability_degraded_flag(_MINT) is True


# ---------------------------------------------------------------------------
# Full loop: SLOW re-probe -> StateStore flag -> FAST ExitEngine SECURE exit.
# ---------------------------------------------------------------------------


def test_degraded_reprobe_then_exit_engine_force_exits_secure():
    """The complete de-risk path: the SLOW re-probe flags a degraded token, and the
    FAST ExitEngine reading that pre-set bool force-exits the remainder SECURE — on a
    fully HEALTHY mark that would otherwise HOLD."""
    store = InMemoryStateStore()
    reprober = SellabilityReprober(
        probe=_FakeProbe(result=_result(SellSimReason.ZERO_OUTPUT, sellable=False)),
        flag_sink=store,
    )
    reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    # FAST branch reads only the bare bool (no sell-sim on the hot path).
    flag = store.get_sellability_degraded_flag(_MINT)
    assert flag is True

    engine = ExitEngine(config=preset("secure_balanced"))
    state = ExitState.at_entry(mint=_MINT, entry_fill_price=_ENTRY, config=engine.config)
    decision = engine.on_tick(
        state,
        mark_price=_ENTRY,  # mark == entry: no stop, no TP -> would HOLD
        block_time_ms=_BT,
        sellability_degraded_flag=flag,
    )
    assert decision.reason is ExitReason.SELLABILITY_DEGRADED
    assert decision.exit_mode is MevExitMode.SECURE
    assert decision.fraction_bps == 10_000
    assert decision.next_state.remaining_bps == 0


def test_healthy_reprobe_then_exit_engine_holds():
    """Control: a healthy re-probe leaves the flag unset, so the SAME healthy mark
    HOLDS (no exit).  Pairs with the degraded test to isolate the branch."""
    store = InMemoryStateStore()
    reprober = SellabilityReprober(
        probe=_FakeProbe(result=_result(SellSimReason.SELLABLE, sellable=True)),
        flag_sink=store,
    )
    reprober.reprobe(_MINT, event_slot=_SLOT, is_open_position=True)

    engine = ExitEngine(config=preset("secure_balanced"))
    state = ExitState.at_entry(mint=_MINT, entry_fill_price=_ENTRY, config=engine.config)
    decision = engine.on_tick(
        state,
        mark_price=_ENTRY,
        block_time_ms=_BT,
        sellability_degraded_flag=store.get_sellability_degraded_flag(_MINT),
    )
    assert decision.reason is ExitReason.NONE
    assert decision.intent is None


# ---------------------------------------------------------------------------
# Batch driver over open positions.
# ---------------------------------------------------------------------------


class _FakePos:
    def __init__(self, mint: str) -> None:
        self.mint = mint


class _FakeStore:
    """Minimal OpenPositionSource — only load_all_positions is required."""

    def __init__(self, mints: list[str]) -> None:
        self._mints = mints

    def load_all_positions(self) -> list:
        return [_FakePos(m) for m in self._mints]


def test_batch_reprobe_over_open_positions():
    store = InMemoryStateStore()
    fake = _FakeStore(["MINT_1", "MINT_2", "MINT_3"])
    reprober = SellabilityReprober(
        probe=_FakeProbe(result=_result(SellSimReason.SIM_REVERT, sellable=False)),
        flag_sink=store,
    )
    results = reprober.reprobe_open_positions(fake, event_slot=_SLOT)
    assert len(results) == 3
    assert all(r.degraded for r in results)
    for m in ("MINT_1", "MINT_2", "MINT_3"):
        assert store.get_sellability_degraded_flag(m) is True


# ---------------------------------------------------------------------------
# Point-in-time / event-time: event_slot is validated as an on-chain int slot.
# ---------------------------------------------------------------------------


def test_reprobe_rejects_bool_event_slot():
    reprober = SellabilityReprober(probe=_FakeProbe(result=_result(SellSimReason.SELLABLE, sellable=True)))
    with pytest.raises(TypeError):
        reprober.reprobe(_MINT, event_slot=True, is_open_position=True)  # type: ignore[arg-type]


def test_reprobe_rejects_negative_event_slot():
    reprober = SellabilityReprober(probe=_FakeProbe(result=_result(SellSimReason.SELLABLE, sellable=True)))
    with pytest.raises(ValueError):
        reprober.reprobe(_MINT, event_slot=-5, is_open_position=True)


def test_default_flag_ttl_matches_insider_dump_default():
    assert DEFAULT_FLAG_TTL_SECONDS == 120


# ---------------------------------------------------------------------------
# REUSE PROOF — the re-probe module defines NO honeypot mechanics of its own.
# ---------------------------------------------------------------------------


def test_reprobe_module_reuses_shared_sellsim_and_forks_nothing():
    """Grep-prove: sellability_reprobe.py imports the SHARED sell-sim and calls
    simulate_sell on the injected probe, and defines NONE of the honeypot mechanics
    (no simulate_transaction, no tax computation, no quote, no sell-tx build).  A
    fork of the mechanics would trip one of these asserts."""
    src = Path("aats/risk/sellability_reprobe.py").read_text(encoding="utf-8")
    # It consumes the shared primitive...
    assert "from aats.execution.sell_sim import" in src
    assert "self._probe.simulate_sell(" in src
    # ...and re-implements none of the honeypot MECHANICS (owned solely by
    # sell_sim.py).  The only `simulate_sell` in this file is the injected-seam
    # Protocol stub (`... : ...`) and the CALL — never a real implementation body.
    forbidden = [
        "simulate_transaction",  # the mandatory pre-send check lives in sell_sim
        "_compute_effective_tax_bps",  # tax math lives in sell_sim
        "_quote_sell",  # quoting lives in sell_sim
        "_build_sell_stub",  # tx assembly lives in sell_sim
        "def _run_simulate",  # the raw simulate wrapper lives in sell_sim
    ]
    for token in forbidden:
        assert token not in src, f"sellability_reprobe.py must not fork honeypot mechanic: {token!r}"
    # The seam declaration is a stub only (body is `...`), not an implementation.
    assert "def simulate_sell(self, *, mint: str, event_slot: int) -> SellSimResult: ..." in src


def test_no_wall_clock_in_reprobe_decision_path():
    """Point-in-time: the module reads no wall-clock into any decision field."""
    src = Path("aats/risk/sellability_reprobe.py").read_text(encoding="utf-8")
    assert not re.search(r"time\.time\(", src)
    assert "datetime.now" not in src
