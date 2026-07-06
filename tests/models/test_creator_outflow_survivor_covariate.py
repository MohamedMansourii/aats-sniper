"""M2-CP-07 — tests proving `creator_outflow_velocity_bps` is correctly wired
into the SLOW-loop survivor model (`aats.models.survivor`) as a de-risk-only,
monotone NON-INCREASING covariate.

Covers (VERIFY criteria from the task brief):
  - the covariate is registered in SURVIVOR_COVARIATE_COLUMNS with a HARD
    monotone -1 (NON-INCREASING) pin
  - `build_survivor_covariate_row` wires it through: absent -> neutral
    sentinel + presence=0 (refuse-by-default, never fabricated); present ->
    passed through exactly
  - the monotone-decreasing constraint is LOAD-BEARING: a LightGBM booster
    trained WITH the shipped -1 constraint, on data with the REAL intended
    (negative) relationship, genuinely DECREASES its prediction as the
    covariate rises -- and a MUTATED (+1) pin trained on the IDENTICAL data
    fails to reproduce that decrease (LightGBM refuses to fit the wrong
    shape against a genuinely opposite signal, so it goes flat), proving the
    test would catch a sign-flip mutation rather than passing vacuously
  - adding the covariate does not break the existing survivor model's
    end-to-end training path (leak audit, calibration, output contract)
"""

from __future__ import annotations

import numpy as np
import pytest

from aats.models.survivor import (
    N_SURVIVOR_COVARIATES,
    SURVIVOR_COVARIATE_COLUMNS,
    SURVIVOR_MONOTONE_NONINCREASING,
    build_survivor_covariate_row,
    survivor_monotone_vector,
    train_survivor_model,
)
from aats.models.synthetic import generate_synthetic_corpus

SEED = 20260706
N = 600

BPS_COL = "creator_outflow_velocity_bps"
PRESENT_COL = "creator_outflow_velocity_present"


@pytest.fixture(scope="module")
def corpus():
    return generate_synthetic_corpus(n=N, seed=SEED)


# ---------------------------------------------------------------------------
# 1. Column registration + monotone pin
# ---------------------------------------------------------------------------


class TestColumnRegistration:
    def test_columns_present(self):
        assert BPS_COL in SURVIVOR_COVARIATE_COLUMNS
        assert PRESENT_COL in SURVIVOR_COVARIATE_COLUMNS
        assert len(SURVIVOR_COVARIATE_COLUMNS) == N_SURVIVOR_COVARIATES

    def test_bps_column_pinned_nonincreasing(self):
        assert BPS_COL in SURVIVOR_MONOTONE_NONINCREASING

    def test_present_flag_column_unconstrained(self):
        """The presence flag itself carries no risk direction -- it must be
        unconstrained (0), never accidentally pinned."""
        assert PRESENT_COL not in SURVIVOR_MONOTONE_NONINCREASING
        from aats.models.survivor import SURVIVOR_MONOTONE_NONDECREASING

        assert PRESENT_COL not in SURVIVOR_MONOTONE_NONDECREASING

    def test_monotone_vector_sign(self):
        mono = survivor_monotone_vector()
        idx = SURVIVOR_COVARIATE_COLUMNS.index(BPS_COL)
        assert mono[idx] == -1
        present_idx = SURVIVOR_COVARIATE_COLUMNS.index(PRESENT_COL)
        assert mono[present_idx] == 0


# ---------------------------------------------------------------------------
# 2. build_survivor_covariate_row wiring
# ---------------------------------------------------------------------------


class TestCovariateRowWiring:
    def test_absent_is_neutral_sentinel(self, corpus):
        row = build_survivor_covariate_row(corpus[0].frame, None)  # default: no 3rd arg
        col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
        assert row[col[BPS_COL]] == 0.0
        assert row[col[PRESENT_COL]] == 0.0

    def test_present_value_passed_through(self, corpus):
        row = build_survivor_covariate_row(corpus[0].frame, None, 6_000.0)
        col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
        assert row[col[BPS_COL]] == 6_000.0
        assert row[col[PRESENT_COL]] == 1.0

    def test_zero_is_a_real_present_value_not_confused_with_absence(self, corpus):
        """A GENUINE reading of 0.0 velocity (creator observed, zero outflow)
        must still set presence=1 -- distinct from `None` (no coverage)."""
        row = build_survivor_covariate_row(corpus[0].frame, None, 0.0)
        col = {c: i for i, c in enumerate(SURVIVOR_COVARIATE_COLUMNS)}
        assert row[col[BPS_COL]] == 0.0
        assert row[col[PRESENT_COL]] == 1.0

    def test_row_length_matches_declared_covariate_count(self, corpus):
        row = build_survivor_covariate_row(corpus[0].frame, None, 1_234.0)
        assert len(row) == N_SURVIVOR_COVARIATES


# ---------------------------------------------------------------------------
# 3. The load-bearing sign test -- LightGBM monotone_constraints is enforced
# ---------------------------------------------------------------------------


class TestMonotoneConstraintIsLoadBearing:
    def test_lightgbm_honors_monotone_constraint_and_a_flipped_pin_would_fail(self):
        """Direct proof (independent of the shared synthetic corpus, which does
        not vary this new covariate) that LightGBM's monotone_constraints=-1
        for `creator_outflow_velocity_bps`, trained on data with the REAL
        intended relationship (higher creator outflow -> lower survival),
        produces a model whose prediction genuinely DECREASES across the
        covariate's range -- and that a MUTATED pin (+1, simulating someone
        flipping the sign in `SURVIVOR_MONOTONE_NONINCREASING`) trained on the
        IDENTICAL data fails to reproduce that decrease (LightGBM refuses to
        fit a monotone-increasing shape against a genuinely decreasing signal,
        so it goes FLAT instead) -- proving this test is mutation-sensitive,
        not vacuously flat regardless of the pin's sign.
        """
        import lightgbm as lgb

        rng = np.random.default_rng(SEED)
        n = 500
        col_idx = SURVIVOR_COVARIATE_COLUMNS.index(BPS_COL)
        present_idx = SURVIVOR_COVARIATE_COLUMNS.index(PRESENT_COL)
        monotone = survivor_monotone_vector()
        assert monotone[col_idx] == -1

        X = np.zeros((n, N_SURVIVOR_COVARIATES), dtype=np.float32)
        outflow_vals = rng.uniform(0, 10_000, size=n).astype(np.float32)
        X[:, col_idx] = outflow_vals
        X[:, present_idx] = 1.0
        # The REAL intended relationship: HIGHER creator outflow -> LOWER
        # P(survive) -- exactly what SURVIVOR_MONOTONE_NONINCREASING pins.
        p_label = 0.9 - 0.7 * (outflow_vals / 10_000.0)
        y = (rng.uniform(0, 1, size=n) < p_label).astype(np.int32)

        base_params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "learning_rate": 0.1,
            "num_leaves": 15,
            "max_depth": 4,
            "min_data_in_leaf": 10,
            "seed": SEED,
            "bagging_seed": SEED,
            "feature_fraction_seed": SEED,
            "deterministic": True,
            "force_row_wise": True,
            "num_threads": 1,
            "verbosity": -1,
        }

        train_set = lgb.Dataset(X, label=y, feature_name=list(SURVIVOR_COVARIATE_COLUMNS))

        sweep = [0.0, 2_500.0, 5_000.0, 7_500.0, 10_000.0]

        def _row(v: float) -> np.ndarray:
            r = np.zeros((1, N_SURVIVOR_COVARIATES), dtype=np.float32)
            r[0, present_idx] = 1.0
            r[0, col_idx] = v
            return r

        # 1. The SHIPPED constraint (-1, read live from survivor_monotone_vector()):
        #    predictions must be non-increasing AND show a genuine de-risk drop.
        constrained_params = dict(base_params, monotone_constraints=monotone)
        booster = lgb.train(constrained_params, train_set, num_boost_round=100)
        preds = [float(booster.predict(_row(v))[0]) for v in sweep]
        for a, b in zip(preds, preds[1:], strict=False):
            assert b <= a + 1e-9, (
                f"prediction INCREASED as creator_outflow_velocity_bps rose: {preds} -- "
                "the monotone NON-INCREASING pin was not honored (forbidden: higher "
                "creator distribution can never raise P(survive))."
            )
        assert preds[0] > preds[-1] + 0.2, (
            "prediction showed NO meaningful de-risk effect across the sweep; the "
            f"shipped -1 pin must produce a genuine decline on this signal, got {preds}"
        )

        # 2. MUTATION GUARD: flip the pin to +1 (simulating a code mutation that
        #    drops/reverses SURVIVOR_MONOTONE_NONINCREASING's entry for this
        #    column) on the SAME data.  LightGBM refuses to fit a rising shape
        #    against a genuinely falling signal -- it goes FLAT (no split),
        #    which fails the "genuine decline" assertion above, proving this
        #    test WOULD catch that mutation.
        flipped_monotone = list(monotone)
        flipped_monotone[col_idx] = 1
        booster_flipped = lgb.train(
            dict(base_params, monotone_constraints=flipped_monotone),
            train_set,
            num_boost_round=100,
        )
        preds_flipped = [float(booster_flipped.predict(_row(v))[0]) for v in sweep]
        assert preds_flipped[0] <= preds_flipped[-1] + 0.05, (
            "sanity check failed: a +1-flipped pin unexpectedly reproduced the shipped "
            f"model's decline ({preds_flipped}) -- the fixture no longer discriminates "
            "the correct sign from a mutated one."
        )


# ---------------------------------------------------------------------------
# 4. End-to-end: adding the covariate does not break the real training path
# ---------------------------------------------------------------------------


class TestSurvivorModelStillTrainsEndToEnd:
    def test_train_survivor_model_runs_with_new_covariate_present(self, corpus):
        model = train_survivor_model(corpus, seed=SEED, num_boost_round=50)
        assert "LEAK AUDIT PASS" in model.leak_audit_summary
        assert model.is_bootstrap_not_real is True
        pred = model.predict(corpus[0].frame, None)
        assert 0.0 <= pred.p_calibrated <= 1.0

    def test_prediction_unaffected_by_absent_vs_explicit_none_default(self, corpus):
        """The new trailing optional arg defaults to None -- a 2-arg call and an
        explicit `None` 3rd-arg call must be indistinguishable (backward
        compatibility for every existing caller)."""
        model = train_survivor_model(corpus, seed=SEED, num_boost_round=50)
        row_default = build_survivor_covariate_row(corpus[0].frame, None)
        row_explicit = build_survivor_covariate_row(corpus[0].frame, None, None)
        assert row_default == row_explicit
        X = np.asarray([row_default, row_explicit], dtype=np.float32)
        p = model.predict_calibrated(X)
        assert p[0] == p[1]
