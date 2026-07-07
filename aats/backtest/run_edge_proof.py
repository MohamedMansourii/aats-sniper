"""`python -m aats.backtest.run_edge_proof` — the runnable GATE-A / GATE-B edge proof.

Loads the recorded corpus, builds the `TradeOutcome` set with the outcome-labeling harness,
and runs the two binary acceptance controls:

  * GATE-A (`aats.models.gate_a.compute_gate_a`)      — does a cohort make money net of cost?
  * GATE-B (`aats.models.gate_b.compute_gate_b_delta`)— does the MODEL beat the frozen
                                                        naive-momentum baseline, net of cost?

FAIL-CLOSED: with ZERO resolved outcomes there is NO metric — the runner prints a NO-DATA
verdict and exits non-zero, NEVER a fabricated pass.  This is exactly why the earlier proof
returned NO-GO: no resolved `TradeOutcome` records existed.

BLOCK_TIME RESOLUTION (T-300a): the decision anchor is the on-chain block_time resolved from
the entry signature via RPC getTransaction (RPC_PRIMARY from the env — never logged).  With
no RPC configured, no record resolves and the runner fails closed (honest, not fabricated).

OFFLINE / NO CAPITAL: reads files + (optionally) a read-only RPC; never signs or lands a
transaction, holds no keypair.  The forward-price source and block_time resolver are
injectable so tests drive it with fixtures and no network.

EXIT CODES: 0 = GO; 3 = NO-GO (verdict computed, did not pass); 2 = FAIL-CLOSED (no resolved
outcomes / no data).

CAPITAL-LICENSING MODE (`--licensing` / `--walk-forward`)
=========================================================
By DEFAULT the GO verdict is the in-sample bootstrap over the whole corpus (i.i.d. for launch/
momentum, source/time-block CLUSTERED for reaction) — a FAST PRE-CHECK, not a capital license (the
charter's "in-sample edge is no edge").  In `--licensing` mode that pre-check is KEPT (and can VETO)
but a GO ADDITIONALLY requires a purged + embargoed forward WALK-FORWARD on the REAL corpus
(`aats.backtest.licensing`): >= 5 forward folds with label-overlap PURGED and a serial-correlation
EMBARGO, whose POOLED out-of-sample GATE-A (model) + GATE-B (delta, effective-sample-gated) pass and
a majority of folds show a positive delta.  The reaction path keeps its source + time-block clustered
resampling INSIDE every fold.  This is the path a future real-data GO must clear to license capital.

STRATEGIES
==========
  * `launch`   (default): decide at the launch instant from t0 economics alone
                (`outcome_harness.build_from_corpus`). The proven, already-run strategy.
  * `momentum` (--strategy momentum [--entry-horizon 60]): WAIT until T_ENTRY seconds, read the
                early price/pressure TRAJECTORY (<= T_ENTRY marks) and decide, then hold to a
                later exit (`momentum_harness.build_momentum_from_corpus`). Same leak-safe PIT
                machinery, same GATE-A / GATE-B controls, moving decision boundary at T_ENTRY.
  * `reaction` (--strategy reaction): FRONT-RUN a PROVEN smart-money/KOL/whale SIGNAL — decide AT
                the on-chain `signal_block_time_ms` (already in the reaction corpus, no RPC needed),
                enter at the (latency-haircut) `signal_price_sol`, and walk the post-signal reaction
                (`reaction_harness.build_reaction_from_corpus`). BASELINE = follow every signal;
                MODEL = a WALK-FORWARD source-reputation filter (decline proven-loser sources). Same
                realizable-exit / cost / GATE-A / GATE-B machinery. Reads `reaction_corpus.jsonl`
                (flat per-signal schema), NOT `labeled_corpus.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from aats.backtest.block_time_cache import (
    DEFAULT_MAX_IN_FLIGHT,
    BlockTimeCache,
    default_cache_path,
    prefetch_from_corpus,
)
from aats.backtest.licensing import (
    DEFAULT_N_FOLDS,
    LICENSING_MIN_FOLDS,
    LicensingResult,
    make_iid_fold_scorer,
    make_reaction_fold_scorer,
    walk_forward_licensing,
)
from aats.backtest.momentum_harness import (
    DEFAULT_ENTRY_HORIZON_S,
    MomentumHarnessStats,
    build_momentum_from_corpus,
)
from aats.backtest.outcome_harness import (
    BlockTimeResolver,
    BlockTimeUnavailable,
    HarnessStats,
    RpcBlockTimeResolver,
    build_from_corpus,
)
from aats.backtest.reaction_gate import (
    build_reaction_clusterings,
    clustered_gate_a,
    clustered_gate_b_delta,
)
from aats.backtest.reaction_harness import (
    ReactionHarnessStats,
    build_reaction_from_corpus,
)
from aats.backtest.realizable_exit import (
    EXIT_MODEL_REALIZABLE,
    EXIT_MODELS,
)
from aats.models.gate_a import compute_gate_a
from aats.models.gate_b import TradeOutcome, UnitOfRisk, compute_gate_b_delta

_DEFAULT_CORPUS = "C:/aats_shadow/labeled_corpus.jsonl"
# The reaction strategy reads a DIFFERENT corpus (flat per-signal schema, T-300a anchor in-record).
_DEFAULT_REACTION_CORPUS = "C:/aats_shadow/reaction_corpus.jsonl"

# Selection strategies the runner can score.
STRATEGY_LAUNCH = "launch"
STRATEGY_MOMENTUM = "momentum"
STRATEGY_REACTION = "reaction"

# Exit codes (documented in the module docstring).
EXIT_GO = 0
EXIT_FAIL_CLOSED = 2
EXIT_NO_GO = 3

# CAPITAL-LICENSING mode: the label horizon (in on-chain SLOTS) used by the purged/embargoed
# walk-forward for the launch/momentum strategies, whose decision anchor is a slot (decision_slot),
# not an ms block_time.  Solana runs ~2.5 slots/s, so ~4500 slots ~= 30 min covers the corpus's
# outer outcome-resolution horizon (up to 1800 s + hold); the purge drops a decision whose label
# window spills past a fold boundary.  Conservative (a longer horizon purges MORE, never fewer).
# The REACTION path does NOT use this — it purges on the exact on-chain ms anchors the harness
# surfaces (`stats.outcome_event_times_ms` / `outcome_label_horizon_end_ms`).
_LAUNCH_MOMENTUM_LABEL_HORIZON_SLOTS = 4500


class _NullBlockTimeResolver:
    """Fail-closed resolver used when no RPC is configured: EVERY record is CENSORED.

    This never fabricates an anchor — with no on-chain block_time source, there are no
    resolved outcomes and the runner fails closed (T-300a)."""

    def resolve(self, signature: str) -> object:
        raise BlockTimeUnavailable(
            "no RPC configured (set RPC_PRIMARY) — cannot resolve on-chain block_time "
            f"for {signature[:12]}... (CENSORED, fail-closed)"
        )


def _resolve_corpus_path(explicit: str | None, strategy: str) -> str:
    """Resolve the corpus path from the CLI `--corpus` value and the strategy.

    An EXPLICITLY-passed `--corpus` is ALWAYS honored (even if it equals the launch default path
    and the strategy is reaction). Only an UNSET `--corpus` (None sentinel) falls back to the
    per-strategy default: the reaction strategy reads its own `reaction_corpus.jsonl`, launch /
    momentum read `labeled_corpus.jsonl`. This avoids the surprising equality-sentinel seam where
    passing the launch default path to `--strategy reaction` was silently swapped.
    """
    if explicit is not None:
        return explicit
    if strategy == STRATEGY_REACTION:
        return _DEFAULT_REACTION_CORPUS
    return _DEFAULT_CORPUS


def _default_resolver(rpc_url: str | None) -> BlockTimeResolver:
    url = rpc_url or os.environ.get("RPC_PRIMARY", "")
    if not url:
        return _NullBlockTimeResolver()  # type: ignore[return-value]
    return RpcBlockTimeResolver(url)


def _maybe_prefetch_resolver(
    resolver: BlockTimeResolver,
    corpus_path: str | Path,
    *,
    cache_path: str | None,
    max_in_flight: int,
) -> BlockTimeResolver:
    """Turn a live (RPC) resolver into a CONCURRENT, DISK-CACHED prefetched resolver.

    This is the perf path (bounded-concurrency getTransaction + a persistent signature cache):
    the harness then resolves every block_time as an O(1) map lookup, with the SAME values and
    the SAME fail-closed censoring as the sequential path.  A fail-closed `_NullBlockTimeResolver`
    (no RPC configured) is passed through unchanged — there is nothing to resolve and the runner
    fails closed exactly as before.
    """
    if isinstance(resolver, _NullBlockTimeResolver):
        return resolver
    cache = BlockTimeCache(cache_path) if cache_path else None
    return prefetch_from_corpus(
        corpus_path, resolver, cache=cache, max_in_flight=max_in_flight
    )


def _run_walk_forward_licensing(
    outcomes: list,
    stats: HarnessStats | MomentumHarnessStats | ReactionHarnessStats,
    *,
    strategy: str,
    n_folds: int,
) -> LicensingResult:
    """Run the purged/embargoed forward walk-forward for the capital-licensing verdict.

    REACTION: purge on the EXACT on-chain ms anchors the harness surfaced
    (`outcome_event_times_ms` / `outcome_label_horizon_end_ms`) and keep the SOURCE + TIME-BLOCK
    CLUSTERED resampling inside every fold (`make_reaction_fold_scorer`).  LAUNCH / MOMENTUM: order
    by `decision_slot` with the declared slot label-horizon and score folds with the shared i.i.d.
    gates (`make_iid_fold_scorer`) — those corpora are not same-source-coupled.
    """
    if strategy == STRATEGY_REACTION:
        event_times = list(getattr(stats, "outcome_event_times_ms", ()) or ())
        label_horizon_ends = list(getattr(stats, "outcome_label_horizon_end_ms", ()) or ())
        source_ids = list(getattr(stats, "outcome_source_ids", ()) or ())
        scorer = make_reaction_fold_scorer(outcomes, source_ids)
    else:
        event_times = [o.decision_slot for o in outcomes]
        label_horizon_ends = [
            o.decision_slot + _LAUNCH_MOMENTUM_LABEL_HORIZON_SLOTS for o in outcomes
        ]
        scorer = make_iid_fold_scorer(outcomes)
    return walk_forward_licensing(
        outcomes,
        event_times,
        label_horizon_ends,
        fold_scorer=scorer,
        n_folds=n_folds,
    )


def _add_licensing_to_scoreboard(scoreboard: dict, result: LicensingResult) -> None:
    """Fold the walk-forward licensing result into the scoreboard (pooled OOS + per-fold + verdict)."""
    scoreboard["licensing_go"] = result.licensing_go
    scoreboard["licensing_reason"] = result.reason
    scoreboard["wf_n_folds_built"] = result.n_folds_built
    scoreboard["wf_n_folds_requested"] = result.n_folds_requested
    scoreboard["wf_min_folds"] = result.min_folds
    scoreboard["wf_folds_sufficient"] = result.folds_sufficient
    scoreboard["wf_embargo"] = result.embargo
    scoreboard["wf_pooled_n"] = result.pooled_n
    scoreboard["wf_folds_positive_delta"] = result.n_folds_positive_delta
    scoreboard["wf_majority_consistent"] = result.majority_consistent
    if result.pooled_gate_a is not None:
        scoreboard["wf_pooled_gate_a"] = result.pooled_gate_a.summary()
        scoreboard["wf_pooled_gate_a_pass"] = result.pooled_gate_a.gate_a_pass
    if result.pooled_gate_b is not None:
        scoreboard["wf_pooled_gate_b"] = result.pooled_gate_b.summary()
        scoreboard["wf_pooled_gate_b_pass"] = result.pooled_gate_b.gate_b_pass
    scoreboard["wf_per_fold"] = [
        {
            "window_id": fs.window_id,
            "n_test": fs.n_test,
            "n_purged": fs.n_purged,
            "n_embargoed": fs.n_embargoed,
            "delta": fs.delta,
            "gate_a_pass": fs.gate_a_pass,
            "gate_b_pass": fs.gate_b_pass,
        }
        for fs in result.per_fold
    ]


def run_edge_proof(
    corpus_path: str | Path,
    *,
    block_time_resolver: BlockTimeResolver,
    out_path: str | Path | None = None,
    strategy: str = STRATEGY_LAUNCH,
    entry_horizon_s: int = DEFAULT_ENTRY_HORIZON_S,
    exit_model: str = EXIT_MODEL_REALIZABLE,
    licensing: bool = False,
    n_folds: int = DEFAULT_N_FOLDS,
) -> tuple[int, dict]:
    """Build the TradeOutcome set and compute the GATE-A / GATE-B verdict.

    Returns (exit_code, scoreboard_dict).  Fail-closed (EXIT_FAIL_CLOSED) when no outcome
    resolves — no metric on no data, never a fabricated pass.

    `strategy` selects the decision boundary:
      * `launch`   — decide at t0 (`build_from_corpus`);
      * `momentum` — decide at `entry_horizon_s` seconds from the early trajectory
                     (`build_momentum_from_corpus`). Both emit the SAME TradeOutcome schema,
                     so GATE-A / GATE-B are computed identically.

    `exit_model` selects the OUTCOME fidelity (DEFAULT `'realizable'`): `'realizable'` haircuts
    the exit fill for liquidity impact + honeypot/unsellable marks (a TRUSTWORTHY, conservative
    verdict — realizable net PnL is always <= spot); `'spot'` keeps the optimistic spot fill
    (parity/regression only). It NEVER changes the selection or the point-in-time leak boundary.

    `licensing` selects the CAPITAL-LICENSING path.  When False (default) the verdict is the
    in-sample bootstrap over the whole corpus — a FAST PRE-CHECK, not a capital license.  When True
    the whole-corpus bootstrap is kept as that pre-check but the GO verdict ADDITIONALLY requires a
    purged + embargoed forward WALK-FORWARD (>= `LICENSING_MIN_FOLDS` folds, label-overlap purged, a
    serial-correlation embargo) whose POOLED out-of-sample GATE-A + GATE-B pass — the real license
    bar ("in-sample edge is no edge").  The reaction path keeps its SOURCE + TIME-BLOCK clustered
    resampling inside every fold.  `n_folds` is how many forward folds to build.
    """
    if exit_model not in EXIT_MODELS:
        raise ValueError(f"unknown exit_model {exit_model!r} (expected one of {EXIT_MODELS})")
    # `stats` is one of three concrete stat types depending on the branch — annotate the union so
    # mypy accepts every assignment (runtime already safe; the scoreboard reads only common /
    # getattr-guarded fields).
    stats: HarnessStats | MomentumHarnessStats | ReactionHarnessStats
    if strategy == STRATEGY_REACTION:
        # FRONT-RUN: the on-chain signal anchor is already in the reaction corpus (no RPC / no
        # block_time_resolver needed) — it is accepted but unused for this strategy.
        outcomes, stats = build_reaction_from_corpus(corpus_path, exit_model=exit_model)
    elif strategy == STRATEGY_MOMENTUM:
        outcomes, stats = build_momentum_from_corpus(
            corpus_path,
            block_time_resolver=block_time_resolver,
            entry_horizon_s=entry_horizon_s,
            exit_model=exit_model,
        )
    elif strategy == STRATEGY_LAUNCH:
        outcomes, stats = build_from_corpus(
            corpus_path, block_time_resolver=block_time_resolver, exit_model=exit_model
        )
    else:
        raise ValueError(
            f"unknown strategy {strategy!r} (expected {STRATEGY_LAUNCH!r}, {STRATEGY_MOMENTUM!r}, "
            f"or {STRATEGY_REACTION!r})"
        )

    scoreboard: dict = {
        "corpus_path": str(corpus_path),
        "strategy": strategy,
        "exit_model": exit_model,
        "n_records": stats.n_records,
        "n_resolved": stats.n_resolved,
        "n_censored": stats.n_censored,
    }
    if strategy == STRATEGY_MOMENTUM:
        scoreboard["entry_horizon_s"] = getattr(stats, "entry_horizon_s", entry_horizon_s)
        scoreboard["n_tradeable"] = getattr(stats, "n_tradeable", None)
        scoreboard["n_skipped_untradeable"] = getattr(stats, "n_skipped_untradeable", None)
    if strategy == STRATEGY_REACTION:
        scoreboard["n_sources"] = getattr(stats, "n_sources", None)
        scoreboard["n_on_neutral_prior"] = getattr(stats, "n_on_neutral_prior", None)
        scoreboard["n_reputation_gated"] = getattr(stats, "n_reputation_gated", None)
        scoreboard["n_declined_by_model"] = getattr(stats, "n_declined_by_model", None)
        # DATA-SUFFICIENCY caveat (surfaced so a null GATE-B delta is not misread as "no skill"):
        # when the reputation gate never fired, the model == the baseline by construction.
        scoreboard["reputation_engaged"] = getattr(stats, "reputation_engaged", None)

    if not outcomes:
        scoreboard["verdict"] = "FAIL-CLOSED (no resolved TradeOutcome records)"
        return EXIT_FAIL_CLOSED, scoreboard

    if strategy == STRATEGY_REACTION:
        # CLUSTERED / BLOCK bootstrap for the reaction CI: reaction signals cluster in time and are
        # same-source-coupled via reputation, so an i.i.d. resample understates variance and could
        # manufacture a lower95>0 on correlated noise (the in-sample capital blocker). The point
        # estimates are still the shared gates'; only the lower-95 bound + pass are clustered. The
        # bound is the conservative MIN over a source AND a time-block clustering.
        source_ids = tuple(getattr(stats, "outcome_source_ids", ()) or ())
        clusterings = build_reaction_clusterings(outcomes, source_ids)
        gate_a_model = clustered_gate_a(outcomes, clusterings, model=True)
        gate_a_baseline = clustered_gate_a(outcomes, clusterings, model=False)
        gate_b = clustered_gate_b_delta(
            outcomes, clusterings, unit_of_risk=UnitOfRisk.NET_PNL_PER_SOL
        )
        scoreboard["gate_ci_method"] = "clustered_bootstrap[source,time_block]"
    else:
        gate_a_model = compute_gate_a(outcomes, model=True)
        gate_a_baseline = compute_gate_a(outcomes, model=False)
        gate_b = compute_gate_b_delta(outcomes, unit_of_risk=UnitOfRisk.NET_PNL_PER_SOL)
        scoreboard["gate_ci_method"] = "iid_bootstrap"

    in_sample_go = bool(gate_a_model.gate_a_pass and gate_b.gate_b_pass)
    scoreboard.update(
        {
            "n_model_selected": sum(1 for o in outcomes if o.model_selected),
            "n_baseline_selected": sum(1 for o in outcomes if o.baseline_selected),
            "n_effective": sum(
                1 for o in outcomes if bool(o.model_selected) != bool(o.baseline_selected)
            ),
            "gate_a_model": gate_a_model.summary(),
            "gate_a_baseline": gate_a_baseline.summary(),
            "gate_b": gate_b.summary(),
            "gate_a_model_pass": gate_a_model.gate_a_pass,
            "gate_b_pass": gate_b.gate_b_pass,
            "gate_b_effective_sample_met": gate_b.effective_sample_met,
        }
    )

    # CAPITAL-LICENSING path: the whole-corpus bootstrap above is only a FAST PRE-CHECK.  A GO now
    # ADDITIONALLY requires the purged + embargoed forward WALK-FORWARD to pass out-of-sample on the
    # REAL corpus ("in-sample edge is no edge").  The in-sample pre-check can still VETO (a GO must
    # clear both), but it can never license on its own.
    if licensing:
        scoreboard["mode"] = "capital-licensing (purged/embargoed walk-forward)"
        scoreboard["licensing_pre_check_pass"] = in_sample_go
        licensing_result = _run_walk_forward_licensing(
            outcomes, stats, strategy=strategy, n_folds=n_folds
        )
        _add_licensing_to_scoreboard(scoreboard, licensing_result)
        go = bool(in_sample_go and licensing_result.licensing_go)
        scoreboard["verdict"] = "GO" if go else "NO-GO"
    else:
        scoreboard["mode"] = "in-sample bootstrap PRE-CHECK (not a capital license)"
        go = in_sample_go
        scoreboard["verdict"] = "GO" if go else "NO-GO"

    if out_path is not None:
        payload = {
            "scoreboard": scoreboard,
            "outcomes": [
                {
                    "mint": o.mint,
                    "decision_slot": o.decision_slot,
                    "model_selected": o.model_selected,
                    "baseline_selected": o.baseline_selected,
                    "net_pnl_lamports": o.net_pnl_lamports,
                    "sol_at_risk_lamports": o.sol_at_risk_lamports,
                }
                for o in outcomes
            ],
        }
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return (EXIT_GO if go else EXIT_NO_GO), scoreboard


def _print_scoreboard(scoreboard: dict) -> None:
    print("\n=== AATS EDGE PROOF (GATE-A / GATE-B) ===")
    print(f"  corpus            : {scoreboard.get('corpus_path')}")
    print(
        f"  exit_model        : {scoreboard.get('exit_model')} "
        "(realizable = liquidity-impact + honeypot haircut; spot = optimistic)"
    )
    strategy = scoreboard.get("strategy", STRATEGY_LAUNCH)
    if strategy == STRATEGY_MOMENTUM:
        print(
            f"  strategy          : momentum (T_ENTRY={scoreboard.get('entry_horizon_s')}s, "
            f"tradeable={scoreboard.get('n_tradeable')}, "
            f"skipped_untradeable={scoreboard.get('n_skipped_untradeable')})"
        )
    elif strategy == STRATEGY_REACTION:
        print(
            f"  strategy          : reaction (front-run; sources={scoreboard.get('n_sources')}, "
            f"neutral_prior={scoreboard.get('n_on_neutral_prior')}, "
            f"reputation_gated={scoreboard.get('n_reputation_gated')}, "
            f"declined_by_model={scoreboard.get('n_declined_by_model')})"
        )
        if scoreboard.get("gate_ci_method"):
            print(f"  gate CI           : {scoreboard.get('gate_ci_method')}")
        if scoreboard.get("reputation_engaged") is False:
            print(
                "  DATA CAVEAT       : reputation gate NEVER fired (0 sources with >= "
                "min_prior_signals observed priors) -> model == baseline by construction; a "
                "GATE-B delta of 0 means the selection filter never got to act, NOT that source "
                "selection has no skill. Reaction edge untestable until repeat-source density grows."
            )
    else:
        print(f"  strategy          : {strategy}")
    print(
        f"  records           : {scoreboard.get('n_records')} "
        f"(resolved={scoreboard.get('n_resolved')}, censored={scoreboard.get('n_censored')})"
    )
    if scoreboard.get("n_resolved", 0) == 0:
        print("  verdict           : " + str(scoreboard.get("verdict")))
        if strategy == STRATEGY_REACTION:
            print(
                "  NOTE: no reaction signals resolved (the reaction corpus is empty/missing, or "
                "every record was censored). The signal anchor is in-record (T-300a) — no RPC "
                "needed. Fail-closed - no metric on no data."
            )
        else:
            print(
                "  NOTE: no on-chain block_time resolved (set RPC_PRIMARY to resolve the corpus "
                "via getTransaction). Fail-closed - no metric on no data."
            )
        print("=========================================\n")
        return
    print(
        f"  selected          : model={scoreboard.get('n_model_selected')} "
        f"baseline={scoreboard.get('n_baseline_selected')}"
    )
    print(f"  {scoreboard.get('gate_a_model')}")
    print(f"  {scoreboard.get('gate_a_baseline')}")
    print(f"  {scoreboard.get('gate_b')}")
    if scoreboard.get("licensing_go") is not None:
        print(
            "  -- CAPITAL-LICENSING WALK-FORWARD (purged + embargoed, out-of-sample) --"
        )
        print(
            f"  pre-check (in-sample bootstrap): "
            f"{'PASS' if scoreboard.get('licensing_pre_check_pass') else 'FAIL'} "
            "(fast pre-check only — NOT a capital license)"
        )
        print(
            f"  folds             : built={scoreboard.get('wf_n_folds_built')}"
            f"/{scoreboard.get('wf_n_folds_requested')} (min {scoreboard.get('wf_min_folds')}, "
            f"embargo={scoreboard.get('wf_embargo')}), "
            f"positive-delta={scoreboard.get('wf_folds_positive_delta')}"
            f" (majority={'yes' if scoreboard.get('wf_majority_consistent') else 'no'})"
        )
        if scoreboard.get("wf_pooled_gate_a"):
            print(f"  pooled OOS n={scoreboard.get('wf_pooled_n')}")
            print(f"  {scoreboard.get('wf_pooled_gate_a')}")
        if scoreboard.get("wf_pooled_gate_b"):
            print(f"  {scoreboard.get('wf_pooled_gate_b')}")
        print(f"  licensing         : {scoreboard.get('licensing_reason')}")
    print(f"  VERDICT           : {scoreboard.get('verdict')}")
    print("=========================================\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aats.backtest.run_edge_proof",
        description=(
            "Build TradeOutcome records from the recorded corpus and run the GATE-A / GATE-B "
            "edge proof. Fail-closed on an empty set. Offline / no capital / no keypair."
        ),
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help=(
            "Path to the corpus JSONL. When left UNSET it defaults per strategy: "
            f"{_DEFAULT_CORPUS} for launch/momentum, {_DEFAULT_REACTION_CORPUS} for "
            "--strategy reaction. An explicitly-passed --corpus is ALWAYS honored."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the scoreboard + TradeOutcome JSON (NOT inside the repo).",
    )
    parser.add_argument(
        "--rpc-url",
        default=None,
        help="RPC URL for getTransaction block_time resolution (default: env RPC_PRIMARY). "
        "Never logged.",
    )
    parser.add_argument(
        "--strategy",
        default=STRATEGY_LAUNCH,
        choices=(STRATEGY_LAUNCH, STRATEGY_MOMENTUM, STRATEGY_REACTION),
        help=(
            f"Selection strategy: {STRATEGY_LAUNCH!r} (decide at t0, default), "
            f"{STRATEGY_MOMENTUM!r} (decide at --entry-horizon seconds from the early "
            f"price/pressure trajectory, then hold), or {STRATEGY_REACTION!r} (FRONT-RUN a proven "
            "smart-money/KOL/whale signal: decide at the on-chain signal anchor, model = "
            f"walk-forward source-reputation filter). {STRATEGY_REACTION!r} reads the reaction "
            "corpus (defaults to the reaction path when --corpus is left unset)."
        ),
    )
    parser.add_argument(
        "--entry-horizon",
        type=int,
        default=DEFAULT_ENTRY_HORIZON_S,
        help=f"Momentum entry horizon T_ENTRY in seconds (default: {DEFAULT_ENTRY_HORIZON_S}). "
        "Only used when --strategy momentum.",
    )
    parser.add_argument(
        "--licensing",
        "--walk-forward",
        dest="licensing",
        action="store_true",
        help=(
            "CAPITAL-LICENSING mode: keep the whole-corpus bootstrap as a fast PRE-CHECK but require "
            "a purged + embargoed forward WALK-FORWARD (>= "
            f"{LICENSING_MIN_FOLDS} folds, label-overlap purged, serial-correlation embargo) whose "
            "POOLED out-of-sample GATE-A + GATE-B pass for a GO. The reaction path keeps its source + "
            "time-block CLUSTERED resampling inside every fold. Without this flag a GO is only the "
            "in-sample pre-check (NOT a capital license)."
        ),
    )
    parser.add_argument(
        "--n-folds",
        type=int,
        default=DEFAULT_N_FOLDS,
        help=f"Number of forward walk-forward folds to build in --licensing mode "
        f"(default: {DEFAULT_N_FOLDS}; a GO needs >= {LICENSING_MIN_FOLDS} non-empty folds).",
    )
    parser.add_argument(
        "--exit-model",
        default=EXIT_MODEL_REALIZABLE,
        choices=EXIT_MODELS,
        help=(
            f"Exit fidelity (default: {EXIT_MODEL_REALIZABLE!r}). {EXIT_MODEL_REALIZABLE!r} haircuts"
            " the exit fill for liquidity impact + honeypot/unsellable marks (a TRUSTWORTHY, "
            "conservative verdict: realizable net PnL is always <= spot). 'spot' keeps the "
            "optimistic spot fill (parity/regression only). Never changes the selection or the "
            "leak boundary."
        ),
    )
    parser.add_argument(
        "--blocktime-cache",
        default=default_cache_path(),
        help="Persistent signature->block_time cache path (default: env BLOCKTIME_CACHE_PATH or "
        "C:/aats_shadow/blocktime_cache.json). A confirmed tx's block_time is immutable, so warm "
        "re-runs are near-instant. Pass '' to disable the disk cache.",
    )
    parser.add_argument(
        "--resolve-concurrency",
        type=int,
        default=DEFAULT_MAX_IN_FLIGHT,
        help="Max in-flight getTransaction calls when resolving block_times concurrently "
        f"(default: {DEFAULT_MAX_IN_FLIGHT}). Deterministic regardless of concurrency.",
    )
    args = parser.parse_args(argv)

    # Resolve the corpus path: an EXPLICIT --corpus is always honored; only an UNSET (None sentinel)
    # --corpus falls back to the per-strategy default (see _resolve_corpus_path).
    corpus = _resolve_corpus_path(args.corpus, args.strategy)

    # The reaction strategy reads a DIFFERENT corpus and needs NO on-chain resolution (the signal
    # anchor is already in-record, T-300a): skip the RPC resolver/prefetch entirely (a fail-closed
    # null resolver is passed but unused).
    if args.strategy == STRATEGY_REACTION:
        resolver: BlockTimeResolver = _NullBlockTimeResolver()  # type: ignore[assignment]
    else:
        resolver = _default_resolver(args.rpc_url)
        # PERF: concurrently prefetch + disk-cache the corpus's block_times (identical values &
        # censoring as the sequential path). A no-RPC fail-closed resolver is passed through as-is.
        resolver = _maybe_prefetch_resolver(
            resolver,
            corpus,
            cache_path=(args.blocktime_cache or None),
            max_in_flight=args.resolve_concurrency,
        )
    exit_code, scoreboard = run_edge_proof(
        corpus,
        block_time_resolver=resolver,
        out_path=args.out,
        strategy=args.strategy,
        entry_horizon_s=args.entry_horizon,
        exit_model=args.exit_model,
        licensing=args.licensing,
        n_folds=args.n_folds,
    )
    _print_scoreboard(scoreboard)
    return exit_code


# Re-export for callers/tests building a fixture-driven proof.
__all__ = [
    "TradeOutcome",
    "run_edge_proof",
    "main",
    "EXIT_GO",
    "EXIT_NO_GO",
    "EXIT_FAIL_CLOSED",
]


if __name__ == "__main__":
    sys.exit(main())
