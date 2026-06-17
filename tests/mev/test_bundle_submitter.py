"""Tests for the atomic buy-with-revert JitoBundleSubmitter (T-330).

Proves:
  - a reverting buy (min-out under-fill) lands NOTHING -> flat state, no orphan,
  - the bundle is atomic: tip + buy ride together; a reverted buy carries no
    landed position,
  - DRY-RUN by default -> no code path reaches the block engine,
  - LIVE is blocked unless DRY_RUN_ENABLED=false AND live enabled,
  - money fields are integer (float rejected at construction).
"""
from __future__ import annotations

import pytest

from aats.contracts.venue import SignedTx
from aats.mev.bundle_submitter import (
    AtomicBuyBundle,
    BundleStatus,
    BundleSubmitMode,
    JitoBundleSubmitter,
    LiveBundleBlocked,
    MockBlockEngine,
)


def _signed_buy(cid="cid-buy-1"):
    return SignedTx(
        serialized_b64="ZmFrZS1zaWduZWQtYnV5",  # fake bytes; never a real key
        signer_pubkey="WalletPubkey1111111111111111111111111111111",
        intent_kind="ENTRY",
        mint="MintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        client_intent_id=cid,
    )


def _bundle(*, min_out=1_000_000, tip=200_000, cid="cid-buy-1"):
    return AtomicBuyBundle(
        signed_buy_tx=_signed_buy(cid),
        tip_lamports=tip,
        tip_account="TipAcct1111111111111111111111111111111111111",
        min_tokens_out_base=min_out,
        client_intent_id=cid,
    )


# ---------------------------------------------------------------------------
# Atomic revert on min-out -> flat state.
# ---------------------------------------------------------------------------


def test_reverting_buy_lands_nothing(monkeypatch):
    monkeypatch.setenv("DRY_RUN_ENABLED", "false")  # allow LIVE for this sim
    # Engine models a buy that would receive only 500k tokens; min-out is 1M.
    engine = MockBlockEngine(actual_tokens_out_base=500_000)
    sub = JitoBundleSubmitter(engine, live_submit_enabled=True)
    assert sub.submit_mode == BundleSubmitMode.LIVE

    result = sub.submit_atomic_buy(_bundle(min_out=1_000_000))

    # The whole bundle reverts: NOTHING lands -> flat state, no orphan position.
    assert result.status is BundleStatus.REVERTED
    assert result.landed_buy is False
    assert "min_out_reverted" in result.reason


def test_sufficient_fill_lands_whole(monkeypatch):
    monkeypatch.setenv("DRY_RUN_ENABLED", "false")
    engine = MockBlockEngine(actual_tokens_out_base=1_200_000)  # >= min_out
    sub = JitoBundleSubmitter(engine, live_submit_enabled=True)
    result = sub.submit_atomic_buy(_bundle(min_out=1_000_000))
    assert result.status is BundleStatus.LANDED
    assert result.landed_buy is True
    assert result.bundle_id is not None


def test_dropped_bundle_is_flat(monkeypatch):
    monkeypatch.setenv("DRY_RUN_ENABLED", "false")
    engine = MockBlockEngine(actual_tokens_out_base=5_000_000, force_drop=True)
    sub = JitoBundleSubmitter(engine, live_submit_enabled=True)
    result = sub.submit_atomic_buy(_bundle(min_out=1_000_000))
    assert result.status is BundleStatus.DROPPED
    assert result.landed_buy is False


def test_atomicity_invariant_blocks_orphan_construction():
    # The BundleResult model itself forbids landed_buy=True without LANDED.
    from aats.mev.bundle_submitter import BundleResult

    with pytest.raises(ValueError):
        BundleResult(
            status=BundleStatus.REVERTED,
            landed_buy=True,  # illegal — would be an orphan position
            bundle_id="x",
            reason="bad",
            tip_lamports=1,
            min_tokens_out_base=1,
        )


# ---------------------------------------------------------------------------
# DRY-RUN by default -> never reaches the block engine.
# ---------------------------------------------------------------------------


class _ExplodingEngine:
    """Any network touch raises — proves DRY_RUN never calls the engine."""

    def send_bundle(self, bundle):  # noqa: ANN001
        raise AssertionError("send_bundle called in DRY_RUN — FR-039 violation!")

    def poll_status(self, bundle_id):  # noqa: ANN001
        raise AssertionError("poll_status called in DRY_RUN — FR-039 violation!")


def test_dry_run_default_never_calls_engine(monkeypatch):
    monkeypatch.delenv("DRY_RUN_ENABLED", raising=False)  # default = dry-run
    sub = JitoBundleSubmitter(_ExplodingEngine())  # live_submit_enabled=False
    assert sub.submit_mode == BundleSubmitMode.DRY_RUN
    result = sub.submit_atomic_buy(_bundle())
    assert result.status is BundleStatus.DRY_RUN
    assert result.landed_buy is False
    assert result.bundle_id is None  # no network call happened


def test_dry_run_even_with_env_false_if_not_enabled(monkeypatch):
    # env says false but live not enabled at construction -> still DRY_RUN.
    monkeypatch.setenv("DRY_RUN_ENABLED", "false")
    sub = JitoBundleSubmitter(_ExplodingEngine(), live_submit_enabled=False)
    assert sub.submit_mode == BundleSubmitMode.DRY_RUN
    result = sub.submit_atomic_buy(_bundle())
    assert result.status is BundleStatus.DRY_RUN


def test_live_blocked_when_env_not_false(monkeypatch):
    # live enabled at construction but env not cleared -> blocked, no submit.
    monkeypatch.setenv("DRY_RUN_ENABLED", "true")
    sub = JitoBundleSubmitter(_ExplodingEngine(), live_submit_enabled=True)
    # submit_mode resolves to DRY_RUN because env gate not cleared.
    assert sub.submit_mode == BundleSubmitMode.DRY_RUN
    result = sub.submit_atomic_buy(_bundle())
    assert result.status is BundleStatus.DRY_RUN


def test_assert_live_allowed_raises_when_forced(monkeypatch):
    # Force the LIVE path assertion directly with env not cleared.
    monkeypatch.setenv("DRY_RUN_ENABLED", "true")
    sub = JitoBundleSubmitter(MockBlockEngine(actual_tokens_out_base=1), live_submit_enabled=True)
    with pytest.raises(LiveBundleBlocked):
        sub._assert_live_allowed("cid-x")


# ---------------------------------------------------------------------------
# Money is integer (float rejected).
# ---------------------------------------------------------------------------


def test_float_tip_rejected():
    with pytest.raises(ValueError):
        AtomicBuyBundle(
            signed_buy_tx=_signed_buy(),
            tip_lamports=1.5,  # type: ignore[arg-type]
            tip_account="TipAcct1",
            min_tokens_out_base=1,
            client_intent_id="cid",
        )


def test_float_min_out_rejected():
    with pytest.raises(ValueError):
        AtomicBuyBundle(
            signed_buy_tx=_signed_buy(),
            tip_lamports=1,
            tip_account="TipAcct1",
            min_tokens_out_base=1.0,  # type: ignore[arg-type]
            client_intent_id="cid",
        )


def test_zero_min_out_rejected_disables_honeypot_guard():
    with pytest.raises(ValueError):
        AtomicBuyBundle(
            signed_buy_tx=_signed_buy(),
            tip_lamports=1,
            tip_account="TipAcct1",
            min_tokens_out_base=0,  # would disable the revert guard
            client_intent_id="cid",
        )


def test_empty_tip_account_rejected():
    with pytest.raises(ValueError):
        AtomicBuyBundle(
            signed_buy_tx=_signed_buy(),
            tip_lamports=1,
            tip_account="",
            min_tokens_out_base=1,
            client_intent_id="cid",
        )
