"""E15 (data-ingestion half) — LEAK-FREE serial-deployer reputation TALLY tests.

Self-check items verified here (per the E15 directive):
  1. A prior RUG whose outcome resolved BEFORE this launch's event_slot counts
     (prior_rugs >= 1).
  2. STRICT point-in-time / no-lookahead: a FUTURE outcome (outcome_slot >=
     as_of_event_slot) is EXCLUDED — explicit leak test, with a same-corpus
     before/after diff proving zero lookahead effect.
  3. A launch can never use its own outcome (outcome_slot == as_of_event_slot
     is excluded — the boundary is strict, not <=).
  4. A clean/unknown creator_wallet returns prior_launches=0, prior_rugs=0 (no
     information, never treated as "verified clean" by this module).
  5. CENSORED prior outcomes are excluded from both tallies (unknown, honestly
     not counted as "survived").
  6. No win_rate / success_rate / rug_rate field anywhere (grep-assert).
  7. Dedup: an outcome indexed under both creator_wallet AND
     deploy_template_fingerprint is counted exactly once.
  8. Malformed corpus construction (outcome_slot <= launch_event_slot) raises.
  9. O(1)-keyed lookup: two distinct wallets never see each other's history.
  10. InMemory vs Parquet backends produce IDENTICAL tallies for identical data
      (drop-in interchangeable, EN5-style parity).
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from aats.contracts.events import LaunchOutcomeLabel
from aats.ingestion.deployer_reputation import (
    PARQUET_DEPLOYER_REQUIRED_COLUMNS,
    DeployerLaunchOutcome,
    DeployerReputationTally,
    InMemoryDeployerOutcomeStore,
    ParquetDeployerOutcomeStore,
    lookup_deployer_reputation,
)

RUGGER = "RuggerWallet1111111111111111111111111111"
CLEAN = "CleanWallet22222222222222222222222222222"
LAUNCH_SLOT = 300_000_000
CURRENT_LAUNCH_SLOT = 300_500_000  # the launch we are deciding on RIGHT NOW


def _outcome(
    creator_wallet: str,
    mint: str,
    launch_event_slot: int,
    outcome_slot: int,
    label: LaunchOutcomeLabel,
    fingerprint: str | None = None,
) -> DeployerLaunchOutcome:
    return DeployerLaunchOutcome(
        creator_wallet=creator_wallet,
        mint=mint,
        launch_event_slot=launch_event_slot,
        outcome_slot=outcome_slot,
        label=label,
        deploy_template_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# 1. A prior RUG resolved before this launch counts
# ---------------------------------------------------------------------------


def test_prior_rug_before_launch_counts():
    store = InMemoryDeployerOutcomeStore(
        [_outcome(RUGGER, "MintA", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, LaunchOutcomeLabel.RUGGED)]
    )
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 1
    assert tally.prior_rugs == 1


# ---------------------------------------------------------------------------
# 2. Explicit LEAK TEST: a future outcome is EXCLUDED
# ---------------------------------------------------------------------------


def test_future_outcome_excluded_leak_test():
    """A rug that resolves AFTER (or exactly at) the decision slot must never
    be visible to the point-in-time lookup for that decision — this is the
    load-bearing no-lookahead guarantee of the whole signal."""
    # Outcome resolves at CURRENT_LAUNCH_SLOT + 5_000 -- strictly AFTER the
    # decision anchor (CURRENT_LAUNCH_SLOT).  Must be excluded.
    future_outcome = _outcome(
        RUGGER,
        "MintFuture",
        CURRENT_LAUNCH_SLOT - 100,
        CURRENT_LAUNCH_SLOT + 5_000,
        LaunchOutcomeLabel.RUGGED,
    )
    store = InMemoryDeployerOutcomeStore([future_outcome])
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0

    # Same-corpus diff: adding a PAST rug changes the tally; the future rug
    # (still present in the store) never does.
    store.add_outcome(
        _outcome(RUGGER, "MintPast", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, LaunchOutcomeLabel.RUGGED)
    )
    tally_with_past = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally_with_past.prior_launches == 1
    assert tally_with_past.prior_rugs == 1  # still NOT 2 -- the future rug never counts


def test_outcome_at_exact_decision_slot_excluded_strict_boundary():
    """A launch can never use its OWN outcome: outcome_slot == as_of_event_slot
    must be excluded (the point-in-time filter is STRICT <, never <=)."""
    store = InMemoryDeployerOutcomeStore(
        [
            _outcome(
                RUGGER,
                "MintSelf",
                CURRENT_LAUNCH_SLOT - 10,
                CURRENT_LAUNCH_SLOT,  # resolves in the SAME slot as the decision
                LaunchOutcomeLabel.RUGGED,
            )
        ]
    )
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0


# ---------------------------------------------------------------------------
# 3. Clean / unknown wallet -> zero tally (no info, not "verified clean")
# ---------------------------------------------------------------------------


def test_unknown_wallet_zero_tally():
    store = InMemoryDeployerOutcomeStore([])
    tally = lookup_deployer_reputation(
        store, creator_wallet=CLEAN, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0


def test_survived_history_counts_launches_not_rugs():
    store = InMemoryDeployerOutcomeStore(
        [
            _outcome(
                CLEAN, "MintOk1", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, LaunchOutcomeLabel.SURVIVED
            ),
            _outcome(
                CLEAN, "MintOk2", LAUNCH_SLOT, LAUNCH_SLOT + 2_000, LaunchOutcomeLabel.SURVIVED
            ),
        ]
    )
    tally = lookup_deployer_reputation(
        store, creator_wallet=CLEAN, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 2
    assert tally.prior_rugs == 0


# ---------------------------------------------------------------------------
# 4. CENSORED prior outcomes are excluded from both tallies
# ---------------------------------------------------------------------------


def test_censored_prior_outcome_excluded():
    store = InMemoryDeployerOutcomeStore(
        [
            _outcome(
                RUGGER,
                "MintCensored",
                LAUNCH_SLOT,
                LAUNCH_SLOT + 1_000,
                LaunchOutcomeLabel.CENSORED,
            )
        ]
    )
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0


# ---------------------------------------------------------------------------
# 5. No win_rate / success_rate / rug_rate field anywhere (grep-assert)
# ---------------------------------------------------------------------------


def test_no_rate_field_on_tally_dataclass():
    field_names = {f.name for f in dataclasses.fields(DeployerReputationTally)}
    for forbidden in ("win_rate", "success_rate", "rug_rate", "hit_rate", "rate"):
        assert forbidden not in field_names
    assert field_names == {
        "creator_wallet",
        "deploy_template_fingerprint",
        "as_of_event_slot",
        "prior_launches",
        "prior_rugs",
    }


def test_no_rate_source_string_anywhere_in_module():
    import inspect

    import aats.ingestion.deployer_reputation as mod

    src = inspect.getsource(mod)
    for forbidden in ("win_rate", "success_rate", "rug_rate", "hit_rate"):
        assert forbidden not in src


# ---------------------------------------------------------------------------
# 6. Dedup across creator_wallet + deploy_template_fingerprint indices
# ---------------------------------------------------------------------------


def test_dedup_across_wallet_and_fingerprint_indices():
    fp = "template-fp-abc123"
    store = InMemoryDeployerOutcomeStore(
        [
            _outcome(
                RUGGER,
                "MintDup",
                LAUNCH_SLOT,
                LAUNCH_SLOT + 1_000,
                LaunchOutcomeLabel.RUGGED,
                fingerprint=fp,
            )
        ]
    )
    tally = lookup_deployer_reputation(
        store,
        creator_wallet=RUGGER,
        as_of_event_slot=CURRENT_LAUNCH_SLOT,
        deploy_template_fingerprint=fp,
    )
    # Must be counted ONCE, not twice, even though the outcome is indexed
    # under both the wallet AND the fingerprint bucket.
    assert tally.prior_launches == 1
    assert tally.prior_rugs == 1


def test_fingerprint_only_outcome_still_found():
    """A record only reachable via the fingerprint index (different wallet
    label, e.g. a wallet-rotation scheme) is still surfaced when a fingerprint
    is supplied -- this is the E-M1-02 forward-compat seam."""
    fp = "template-fp-rotated"
    store = InMemoryDeployerOutcomeStore(
        [
            _outcome(
                "SomeOtherWallet",
                "MintRotated",
                LAUNCH_SLOT,
                LAUNCH_SLOT + 1_000,
                LaunchOutcomeLabel.RUGGED,
                fingerprint=fp,
            )
        ]
    )
    tally = lookup_deployer_reputation(
        store,
        creator_wallet=RUGGER,  # no history under RUGGER itself
        as_of_event_slot=CURRENT_LAUNCH_SLOT,
        deploy_template_fingerprint=fp,
    )
    assert tally.prior_launches == 1
    assert tally.prior_rugs == 1


# ---------------------------------------------------------------------------
# 7. Malformed corpus construction raises (fail fast, never silently trusted)
# ---------------------------------------------------------------------------


def test_outcome_resolving_before_its_own_launch_raises():
    with pytest.raises(ValueError, match="outcome_slot"):
        _outcome(RUGGER, "MintBad", LAUNCH_SLOT, LAUNCH_SLOT - 1, LaunchOutcomeLabel.RUGGED)


def test_outcome_resolving_same_slot_as_its_own_launch_raises():
    with pytest.raises(ValueError, match="outcome_slot"):
        _outcome(RUGGER, "MintBad2", LAUNCH_SLOT, LAUNCH_SLOT, LaunchOutcomeLabel.RUGGED)


def test_tally_rejects_prior_rugs_exceeding_prior_launches():
    with pytest.raises(ValueError, match="cannot exceed"):
        DeployerReputationTally(
            creator_wallet=RUGGER,
            deploy_template_fingerprint=None,
            as_of_event_slot=CURRENT_LAUNCH_SLOT,
            prior_launches=1,
            prior_rugs=2,
        )


def test_tally_rejects_negative_counts():
    with pytest.raises(ValueError):
        DeployerReputationTally(
            creator_wallet=RUGGER,
            deploy_template_fingerprint=None,
            as_of_event_slot=CURRENT_LAUNCH_SLOT,
            prior_launches=-1,
            prior_rugs=0,
        )


# ---------------------------------------------------------------------------
# 8. O(1)-keyed: two wallets never see each other's history
# ---------------------------------------------------------------------------


def test_wallets_are_isolated():
    store = InMemoryDeployerOutcomeStore(
        [
            _outcome(RUGGER, "MintR", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, LaunchOutcomeLabel.RUGGED),
            _outcome(CLEAN, "MintC", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, LaunchOutcomeLabel.SURVIVED),
        ]
    )
    rugger_tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    clean_tally = lookup_deployer_reputation(
        store, creator_wallet=CLEAN, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert rugger_tally.prior_rugs == 1
    assert clean_tally.prior_rugs == 0
    assert clean_tally.prior_launches == 1


# ---------------------------------------------------------------------------
# 9. InMemory vs Parquet parity (EN5-style)
# ---------------------------------------------------------------------------


def _write_parquet(path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(path)


def _row(
    creator_wallet: str,
    mint: str,
    launch_event_slot: int,
    outcome_slot: int,
    label: str,
    fingerprint: str | None = None,
) -> dict:
    return {
        "creator_wallet": creator_wallet,
        "mint": mint,
        "launch_event_slot": launch_event_slot,
        "outcome_slot": outcome_slot,
        "label": label,
        "deploy_template_fingerprint": fingerprint,
    }


def test_parquet_and_inmemory_produce_identical_tallies(tmp_path):
    rows = [
        _row(RUGGER, "MintA", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, "RUGGED"),
        _row(RUGGER, "MintB", LAUNCH_SLOT, LAUNCH_SLOT + 2_000, "SURVIVED"),
        _row(CLEAN, "MintC", LAUNCH_SLOT, LAUNCH_SLOT + 1_500, "SURVIVED"),
    ]
    path = tmp_path / "deployer_outcomes.parquet"
    _write_parquet(path, rows)

    parquet_store = ParquetDeployerOutcomeStore(path=str(path))
    memory_store = InMemoryDeployerOutcomeStore(
        [
            _outcome(RUGGER, "MintA", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, LaunchOutcomeLabel.RUGGED),
            _outcome(
                RUGGER, "MintB", LAUNCH_SLOT, LAUNCH_SLOT + 2_000, LaunchOutcomeLabel.SURVIVED
            ),
            _outcome(CLEAN, "MintC", LAUNCH_SLOT, LAUNCH_SLOT + 1_500, LaunchOutcomeLabel.SURVIVED),
        ]
    )

    for wallet in (RUGGER, CLEAN):
        p_tally = lookup_deployer_reputation(
            parquet_store, creator_wallet=wallet, as_of_event_slot=CURRENT_LAUNCH_SLOT
        )
        m_tally = lookup_deployer_reputation(
            memory_store, creator_wallet=wallet, as_of_event_slot=CURRENT_LAUNCH_SLOT
        )
        assert p_tally.prior_launches == m_tally.prior_launches
        assert p_tally.prior_rugs == m_tally.prior_rugs


def test_parquet_store_missing_path_returns_empty_never_raises():
    store = ParquetDeployerOutcomeStore(path=None)
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0


def test_parquet_store_nonexistent_path_returns_empty_never_raises(tmp_path):
    store = ParquetDeployerOutcomeStore(path=str(tmp_path / "does-not-exist.parquet"))
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0


def test_parquet_store_missing_required_column_returns_empty(tmp_path):
    path = tmp_path / "bad_schema.parquet"
    pd.DataFrame([{"creator_wallet": RUGGER, "mint": "M"}]).to_parquet(path)
    store = ParquetDeployerOutcomeStore(path=str(path))
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
    assert tally.prior_rugs == 0
    assert PARQUET_DEPLOYER_REQUIRED_COLUMNS  # non-empty contract


def test_parquet_store_drops_pit_violating_row_never_crashes(tmp_path):
    rows = [
        _row(RUGGER, "MintBad", LAUNCH_SLOT, LAUNCH_SLOT - 1, "RUGGED"),  # PIT violation
        _row(RUGGER, "MintGood", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, "RUGGED"),
    ]
    path = tmp_path / "mixed.parquet"
    _write_parquet(path, rows)
    store = ParquetDeployerOutcomeStore(path=str(path))
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    # Only the good row counts -- the PIT-violating row is dropped, not trusted.
    assert tally.prior_launches == 1
    assert tally.prior_rugs == 1


def test_parquet_store_reload_picks_up_new_rows(tmp_path):
    path = tmp_path / "reload.parquet"
    _write_parquet(path, [_row(RUGGER, "MintA", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, "RUGGED")])
    store = ParquetDeployerOutcomeStore(path=str(path))
    tally_before = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally_before.prior_rugs == 1

    _write_parquet(
        path,
        [
            _row(RUGGER, "MintA", LAUNCH_SLOT, LAUNCH_SLOT + 1_000, "RUGGED"),
            _row(RUGGER, "MintB", LAUNCH_SLOT, LAUNCH_SLOT + 2_000, "RUGGED"),
        ],
    )
    # Without reload(), the cached read is stable.
    tally_stale = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally_stale.prior_rugs == 1

    store.reload()
    tally_after = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally_after.prior_rugs == 2


# ---------------------------------------------------------------------------
# 10. from_env placeholder handling
# ---------------------------------------------------------------------------


def test_from_env_placeholder_treated_as_unset():
    store = ParquetDeployerOutcomeStore.from_env(
        {
            "DEPLOYER_OUTCOME_STORE_PATH": "<path-to-labels-parquet-e.g. data/labels/deployer_outcomes>"
        }
    )
    tally = lookup_deployer_reputation(
        store, creator_wallet=RUGGER, as_of_event_slot=CURRENT_LAUNCH_SLOT
    )
    assert tally.prior_launches == 0
