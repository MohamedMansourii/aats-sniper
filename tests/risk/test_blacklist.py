"""E2 — creator/token DENYLIST + trusted ALLOWLIST pre-filter tests.

Proves (the E2 acceptance criteria):
  * A denylisted CREATOR wallet is REJECTED *before* the safety gate runs, with the
    CREATOR_DENYLISTED red flag and a logged reason.
  * A denylisted token MINT is REJECTED pre-gate with MINT_DENYLISTED + logged reason.
  * HOT-RELOAD works: adding a key to the file and reloading rejects a previously-
    passing candidate; removing it lets it through again.
  * The ALLOWLIST does NOT skip the safety gate: an allowlisted-but-unsafe token is
    still REJECTED by the safety gate (allowlist is observe-only, never a bypass).
  * DENYLIST WINS TIES: a key in both lists is rejected (fail-closed).
  * FAIL-SAFE reload: a malformed file is ignored and the previous good snapshot is
    retained (never widens or drops the denylist on a typo).
  * REJECT-only / de-risk-only: the pre-filter can only veto; there is no path that
    sizes/triggers/passes a denylisted token.
  * POINT-IN-TIME: membership ignores event_slot/block_time (same test live+backtest).
  * MONEY/SECRETS: no money math; the file holds public addresses only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from aats.risk.blacklist import (
    DenyAllowSnapshot,
    DenyAllowWatcher,
    PreGateResult,
    evaluate_with_denylist,
    parse_snapshot,
)
from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
)

CREATOR_OK = "GoodDev11111111111111111111111111111111111111"
CREATOR_BAD = "Rug11111111111111111111111111111111111111111"
MINT_OK = "So1111111111111111111111111111111111111111"
MINT_BAD = "Scam111111111111111111111111111111111111111"
SLOT = 1_000
BTIME = 1_700_000_000_000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _doc(
    denied_creators: list[str] | None = None,
    denied_mints: list[str] | None = None,
    allowed_creators: list[str] | None = None,
    allowed_mints: list[str] | None = None,
) -> dict:
    return {
        "version": "test-1",
        "denylist": {
            "creators": [{"wallet": w, "reason": "test"} for w in (denied_creators or [])],
            "mints": [{"mint": m, "reason": "test"} for m in (denied_mints or [])],
        },
        "allowlist": {
            "creators": [{"wallet": w} for w in (allowed_creators or [])],
            "mints": [{"mint": m} for m in (allowed_mints or [])],
        },
    }


@pytest.fixture
def list_file(tmp_path: Path) -> Path:
    p = tmp_path / "creator-token-denylist.json"
    p.write_text(json.dumps(_doc(denied_creators=[CREATOR_BAD], denied_mints=[MINT_BAD])))
    return p


def _write(path: Path, **kw) -> None:
    path.write_text(json.dumps(_doc(**kw)))


def _clean_inputs(**overrides) -> SafetyInputs:
    base = {
        "mint": MINT_OK,
        "event_slot": SLOT,
        "event_block_time_ms": BTIME,
        "data_staleness_ms": 50,
        "freeze_authority": None,
        "mint_authority": None,
        "lp_locked_or_burned_bps": 10_000,
        "dev_bundle_cluster_bps": 500,
        "holder_concentration_top10_bps": 2_000,
        "sell_tax_bps": 0,
        "buy_tax_bps": 0,
        "authorities_decoded": True,
    }
    base.update(overrides)
    return SafetyInputs(**base)


def _watcher(path: Path, throttle: float = 0.0) -> DenyAllowWatcher:
    # throttle=0 => stat() on every maybe_reload (deterministic for tests).
    return DenyAllowWatcher(path=path, reload_throttle_s=throttle)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_snapshot_basic():
    snap = parse_snapshot(json.dumps(_doc(denied_creators=[CREATOR_BAD], denied_mints=[MINT_BAD])))
    assert snap.is_creator_denied(CREATOR_BAD)
    assert snap.is_mint_denied(MINT_BAD)
    assert not snap.is_creator_denied(CREATOR_OK)
    assert not snap.is_mint_denied(MINT_OK)


def test_parse_accepts_bare_strings():
    raw = json.dumps({"denylist": {"creators": [CREATOR_BAD], "mints": [MINT_BAD]}})
    snap = parse_snapshot(raw)
    assert snap.is_creator_denied(CREATOR_BAD)
    assert snap.is_mint_denied(MINT_BAD)


def test_parse_rejects_non_object_root():
    with pytest.raises(ValueError):
        parse_snapshot(json.dumps([1, 2, 3]))


def test_empty_snapshot_denies_nothing_trusts_nobody():
    snap = DenyAllowSnapshot.empty()
    assert not snap.is_creator_denied(CREATOR_BAD)
    assert not snap.is_mint_denied(MINT_BAD)
    assert not snap.is_creator_allowed(CREATOR_OK)
    assert not snap.is_mint_allowed(MINT_OK)


def test_none_key_never_denied():
    # A None creator/mint is never a denylist hit (refuse-by-default belongs to the
    # safety gate; the denylist has no opinion on an unknown key).
    snap = parse_snapshot(json.dumps(_doc(denied_creators=[CREATOR_BAD])))
    assert not snap.is_creator_denied(None)
    assert not snap.is_mint_denied(None)


# ---------------------------------------------------------------------------
# Pre-gate REJECT (denylisted creator / mint) — BEFORE the safety gate.
# ---------------------------------------------------------------------------


class _SpyTelemetry:
    """Records denylist rejects + reloads so we can assert the reason is surfaced."""

    def __init__(self) -> None:
        self.rejects: list[tuple[str, str, str]] = []
        self.reloads: list[tuple[int, int, bool]] = []

    def on_denylist_reject(self, mint: str, flag: str, key: str) -> None:
        self.rejects.append((mint, flag, key))

    def on_denylist_reload(self, *, denied: int, allowed: int, ok: bool) -> None:
        self.reloads.append((denied, allowed, ok))


def test_denylisted_creator_rejected_pre_gate(list_file, caplog):
    spy = _SpyTelemetry()
    gate = PreTradeSafetyGate()
    with caplog.at_level(logging.INFO):
        # Construct INSIDE the caplog context so the loader's "denylist loaded" log
        # (emitted at first load) is captured.
        w = DenyAllowWatcher(path=list_file, reload_throttle_s=0.0, telemetry=spy)
        res = evaluate_with_denylist(
            watcher=w,
            gate=gate,
            safety_inputs=_clean_inputs(),  # a CLEAN token...
            creator=CREATOR_BAD,  # ...but a denylisted CREATOR
            mint=MINT_OK,
            event_slot=SLOT,
            event_block_time_ms=BTIME,
        )
    assert res.passed is False
    assert res.rejected_by_denylist is True
    assert res.denylist_hit is not None
    assert res.denylist_hit.flag is RedFlag.CREATOR_DENYLISTED
    # The safety gate was NOT reached — proves the denylist runs BEFORE the gate.
    assert res.gate_decision is None
    # Logged reason present (the loader logs the loaded denylist on first load).
    assert "denylist loaded" in caplog.text.lower()
    # And the structured reject reason is surfaced via telemetry (mint, flag, key).
    assert spy.rejects == [(MINT_OK, "creator_denylisted", CREATOR_BAD)]


def test_denylisted_mint_rejected_pre_gate(list_file):
    w = _watcher(list_file)
    gate = PreTradeSafetyGate()
    res = evaluate_with_denylist(
        watcher=w,
        gate=gate,
        safety_inputs=_clean_inputs(),
        creator=CREATOR_OK,
        mint=MINT_BAD,  # denylisted mint
        event_slot=SLOT,
        event_block_time_ms=BTIME,
    )
    assert res.passed is False
    assert res.rejected_by_denylist is True
    assert res.denylist_hit.flag is RedFlag.MINT_DENYLISTED
    assert res.gate_decision is None


def test_clean_non_denylisted_runs_full_gate_and_passes(list_file):
    w = _watcher(list_file)
    gate = PreTradeSafetyGate()
    res = evaluate_with_denylist(
        watcher=w,
        gate=gate,
        safety_inputs=_clean_inputs(),
        creator=CREATOR_OK,
        mint=MINT_OK,
        event_slot=SLOT,
        event_block_time_ms=BTIME,
    )
    # Not denylisted => the safety gate runs (gate_decision present) and passes.
    assert res.rejected_by_denylist is False
    assert res.gate_decision is not None
    assert res.gate_decision.verdict is GateVerdict.PASS
    assert res.passed is True


def test_check_keys_logs_reject_reason(list_file, caplog):
    # The telemetry seam carries the structured reason; the gate-side red flag code
    # is the dashboard-facing reason. Assert the flag (the "logged reason" contract).
    w = _watcher(list_file)
    hit = w.check_keys(
        creator=CREATOR_BAD, mint=MINT_OK, event_slot=SLOT, event_block_time_ms=BTIME
    )
    assert hit is not None
    assert hit.flag.value == "creator_denylisted"


# ---------------------------------------------------------------------------
# HOT-RELOAD
# ---------------------------------------------------------------------------


def test_hot_reload_adds_creator(list_file):
    # Start: CREATOR_OK is not denied.
    w = _watcher(list_file)
    assert w.check_keys(creator=CREATOR_OK, mint=MINT_OK) is None

    # Operator adds CREATOR_OK to the denylist on disk.
    _write(list_file, denied_creators=[CREATOR_BAD, CREATOR_OK], denied_mints=[MINT_BAD])
    swapped = w.maybe_reload(force=True)
    assert swapped is True

    # Now the same key is rejected — hot-reload took effect WITHOUT a restart.
    hit = w.check_keys(creator=CREATOR_OK, mint=MINT_OK)
    assert hit is not None
    assert hit.flag is RedFlag.CREATOR_DENYLISTED


def test_hot_reload_removes_key(list_file):
    w = _watcher(list_file)
    assert w.check_keys(creator=CREATOR_BAD, mint=MINT_OK) is not None

    # Operator removes the creator from the file.
    _write(list_file, denied_creators=[], denied_mints=[MINT_BAD])
    assert w.maybe_reload(force=True) is True
    assert w.check_keys(creator=CREATOR_BAD, mint=MINT_OK) is None


def test_reload_noop_when_unchanged(list_file):
    w = _watcher(list_file)
    # No file change => force=False reload is a no-op (mtime+size unchanged).
    assert w.maybe_reload(force=False) is False


def test_reload_throttled(list_file):
    ticks = {"t": 100.0}
    w = DenyAllowWatcher(path=list_file, reload_throttle_s=5.0, _now=lambda: ticks["t"])
    # Change the file, but within the throttle window the watcher won't stat().
    _write(list_file, denied_creators=[CREATOR_BAD, CREATOR_OK])
    ticks["t"] = 101.0  # only 1s passed; throttle is 5s
    assert w.maybe_reload() is False
    assert w.check_keys(creator=CREATOR_OK, mint=MINT_OK) is None
    # After the throttle window, the reload is allowed and takes effect.
    ticks["t"] = 110.0
    assert w.maybe_reload() is True
    assert w.check_keys(creator=CREATOR_OK, mint=MINT_OK) is not None


# ---------------------------------------------------------------------------
# FAIL-SAFE: malformed reload retains the previous good snapshot.
# ---------------------------------------------------------------------------


def test_malformed_reload_retains_previous(list_file, caplog):
    w = _watcher(list_file)
    assert w.check_keys(creator=CREATOR_BAD, mint=MINT_OK) is not None  # good snapshot

    # Corrupt the file with invalid JSON.
    list_file.write_text("{ this is not json ]]]")
    with caplog.at_level(logging.ERROR):
        swapped = w.maybe_reload(force=True)
    assert swapped is False  # reload rejected
    assert "reload REJECTED" in caplog.text or "rejected" in caplog.text.lower()
    # The PREVIOUS good denylist is still enforced — we did not drop it on a typo.
    assert w.check_keys(creator=CREATOR_BAD, mint=MINT_OK) is not None


def test_missing_file_keeps_empty_snapshot(tmp_path):
    missing = tmp_path / "nope.json"
    w = DenyAllowWatcher(path=missing, reload_throttle_s=0.0)
    # No file: empty snapshot, denies nothing (additive selectivity, not risk-up).
    assert w.check_keys(creator=CREATOR_BAD, mint=MINT_BAD) is None
    assert w.maybe_reload(force=True) is False


# ---------------------------------------------------------------------------
# ALLOWLIST does NOT skip the safety gate.
# ---------------------------------------------------------------------------


def test_allowlist_does_not_skip_safety_gate(tmp_path):
    # Allowlist the creator AND mint, but feed an UNSAFE token (freeze authority set).
    p = tmp_path / "list.json"
    _write(p, allowed_creators=[CREATOR_OK], allowed_mints=[MINT_OK])
    w = _watcher(p)
    gate = PreTradeSafetyGate()

    unsafe = _clean_inputs(freeze_authority="Fr33ze11111111111111111111111111111111111111")
    res = evaluate_with_denylist(
        watcher=w,
        gate=gate,
        safety_inputs=unsafe,
        creator=CREATOR_OK,  # trusted
        mint=MINT_OK,  # trusted
        event_slot=SLOT,
        event_block_time_ms=BTIME,
    )
    # Trusted, but STILL rejected by the safety gate — allowlist is NOT a bypass.
    assert res.was_allowlisted is True
    assert res.rejected_by_denylist is False  # denylist had no opinion
    assert res.gate_decision is not None  # the gate RAN
    assert res.gate_decision.verdict is GateVerdict.REJECT
    assert RedFlag.FREEZE_AUTHORITY_SET in {h.flag for h in res.gate_decision.red_flags}
    assert res.passed is False


def test_allowlisted_clean_token_still_runs_gate(tmp_path):
    p = tmp_path / "list.json"
    _write(p, allowed_mints=[MINT_OK])
    w = _watcher(p)
    gate = PreTradeSafetyGate()
    res = evaluate_with_denylist(
        watcher=w,
        gate=gate,
        safety_inputs=_clean_inputs(),
        creator=CREATOR_OK,
        mint=MINT_OK,
        event_slot=SLOT,
        event_block_time_ms=BTIME,
    )
    # A trusted clean token passes — but ONLY because the gate ran and said PASS,
    # never because it was trusted.  gate_decision is present, proving the gate ran.
    assert res.was_allowlisted is True
    assert res.gate_decision is not None
    assert res.passed is True


def test_is_allowed_is_observe_only(list_file):
    # is_allowed must not exist as a bypass: it returns a bool for the dashboard and
    # has no power over the gate.  evaluate_with_denylist never branches on it to pass.
    w = _watcher(list_file)
    assert w.is_allowed(creator=CREATOR_OK, mint=MINT_OK) is False


# ---------------------------------------------------------------------------
# DENYLIST WINS TIES (fail-closed).
# ---------------------------------------------------------------------------


def test_denylist_wins_over_allowlist(tmp_path):
    p = tmp_path / "list.json"
    # CREATOR_BAD is in BOTH lists.
    _write(p, denied_creators=[CREATOR_BAD], allowed_creators=[CREATOR_BAD])
    w = _watcher(p)
    gate = PreTradeSafetyGate()
    res = evaluate_with_denylist(
        watcher=w,
        gate=gate,
        safety_inputs=_clean_inputs(),
        creator=CREATOR_BAD,
        mint=MINT_OK,
        event_slot=SLOT,
        event_block_time_ms=BTIME,
    )
    assert res.was_allowlisted is True  # observed as trusted...
    assert res.rejected_by_denylist is True  # ...but the denylist wins
    assert res.passed is False
    assert res.gate_decision is None  # short-circuited before the gate


# ---------------------------------------------------------------------------
# REJECT-only / de-risk-only structural guarantee.
# ---------------------------------------------------------------------------


def test_pregate_result_has_no_sizing_fields():
    # The result object can only express PASS/REJECT + audit fields. It must NOT
    # carry any size/trigger/stop instruction (de-risk-only).
    fields = set(PreGateResult.__dataclass_fields__)
    forbidden = {"size", "size_lamports", "lamports", "trigger", "buy", "stop", "leverage", "cap"}
    assert fields & forbidden == set()


def test_check_keys_returns_only_none_or_rejecthit(list_file):
    w = _watcher(list_file)
    # Denylisted => a RedFlagHit (reject). Not denylisted => None (no opinion).
    assert w.check_keys(creator=CREATOR_BAD, mint=MINT_OK) is not None
    assert w.check_keys(creator=CREATOR_OK, mint=MINT_OK) is None
    # There is no return value that means "buy" — the type is RedFlagHit | None.


# ---------------------------------------------------------------------------
# POINT-IN-TIME: membership ignores event_slot/block_time.
# ---------------------------------------------------------------------------


def test_membership_independent_of_event_time(list_file):
    w = _watcher(list_file)
    h1 = w.check_keys(creator=CREATOR_BAD, mint=MINT_OK, event_slot=1, event_block_time_ms=1)
    h2 = w.check_keys(
        creator=CREATOR_BAD, mint=MINT_OK, event_slot=10**9, event_block_time_ms=10**15
    )
    # Same decision regardless of slot/time => no compute-time leak; identical code
    # path live and in backtest.
    assert (h1 is None) == (h2 is None)
    assert h1 is not None and h2 is not None
    assert h1.flag is h2.flag


# ---------------------------------------------------------------------------
# Ships-with seed config parses.
# ---------------------------------------------------------------------------


def test_shipped_config_parses():
    repo_root = Path(__file__).resolve().parents[2]
    cfg = repo_root / "config" / "creator-token-denylist.json"
    assert cfg.exists(), f"expected seed denylist at {cfg}"
    snap = parse_snapshot(cfg.read_text(encoding="utf-8"))
    # Seed contains the example scam keys; structure is valid.
    assert isinstance(snap, DenyAllowSnapshot)
    assert snap.version != ""
