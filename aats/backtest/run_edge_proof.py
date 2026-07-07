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

EXIT CODES: 0 = GO (GATE-A model PASS and GATE-B PASS); 3 = NO-GO (verdict computed, not both
pass); 2 = FAIL-CLOSED (no resolved outcomes / no data).

STRATEGIES
==========
  * `launch`   (default): decide at the launch instant from t0 economics alone
                (`outcome_harness.build_from_corpus`). The proven, already-run strategy.
  * `momentum` (--strategy momentum [--entry-horizon 60]): WAIT until T_ENTRY seconds, read the
                early price/pressure TRAJECTORY (<= T_ENTRY marks) and decide, then hold to a
                later exit (`momentum_harness.build_momentum_from_corpus`). Same leak-safe PIT
                machinery, same GATE-A / GATE-B controls, moving decision boundary at T_ENTRY.
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
from aats.backtest.realizable_exit import (
    EXIT_MODEL_REALIZABLE,
    EXIT_MODELS,
)
from aats.models.gate_a import compute_gate_a
from aats.models.gate_b import TradeOutcome, UnitOfRisk, compute_gate_b_delta

_DEFAULT_CORPUS = "C:/aats_shadow/labeled_corpus.jsonl"

# Selection strategies the runner can score.
STRATEGY_LAUNCH = "launch"
STRATEGY_MOMENTUM = "momentum"

# Exit codes (documented in the module docstring).
EXIT_GO = 0
EXIT_FAIL_CLOSED = 2
EXIT_NO_GO = 3


class _NullBlockTimeResolver:
    """Fail-closed resolver used when no RPC is configured: EVERY record is CENSORED.

    This never fabricates an anchor — with no on-chain block_time source, there are no
    resolved outcomes and the runner fails closed (T-300a)."""

    def resolve(self, signature: str) -> object:
        raise BlockTimeUnavailable(
            "no RPC configured (set RPC_PRIMARY) — cannot resolve on-chain block_time "
            f"for {signature[:12]}... (CENSORED, fail-closed)"
        )


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


def run_edge_proof(
    corpus_path: str | Path,
    *,
    block_time_resolver: BlockTimeResolver,
    out_path: str | Path | None = None,
    strategy: str = STRATEGY_LAUNCH,
    entry_horizon_s: int = DEFAULT_ENTRY_HORIZON_S,
    exit_model: str = EXIT_MODEL_REALIZABLE,
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
    """
    if exit_model not in EXIT_MODELS:
        raise ValueError(f"unknown exit_model {exit_model!r} (expected one of {EXIT_MODELS})")
    # `stats` is one of two concrete stat types depending on the branch — annotate the union so
    # mypy accepts both assignments (runtime already safe; the scoreboard reads only common /
    # getattr-guarded fields).
    stats: HarnessStats | MomentumHarnessStats
    if strategy == STRATEGY_MOMENTUM:
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
            f"unknown strategy {strategy!r} (expected {STRATEGY_LAUNCH!r} or {STRATEGY_MOMENTUM!r})"
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

    if not outcomes:
        scoreboard["verdict"] = "FAIL-CLOSED (no resolved TradeOutcome records)"
        return EXIT_FAIL_CLOSED, scoreboard

    gate_a_model = compute_gate_a(outcomes, model=True)
    gate_a_baseline = compute_gate_a(outcomes, model=False)
    gate_b = compute_gate_b_delta(outcomes, unit_of_risk=UnitOfRisk.NET_PNL_PER_SOL)

    go = bool(gate_a_model.gate_a_pass and gate_b.gate_b_pass)
    scoreboard.update(
        {
            "n_model_selected": sum(1 for o in outcomes if o.model_selected),
            "n_baseline_selected": sum(1 for o in outcomes if o.baseline_selected),
            "gate_a_model": gate_a_model.summary(),
            "gate_a_baseline": gate_a_baseline.summary(),
            "gate_b": gate_b.summary(),
            "gate_a_model_pass": gate_a_model.gate_a_pass,
            "gate_b_pass": gate_b.gate_b_pass,
            "verdict": "GO" if go else "NO-GO",
        }
    )

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
    else:
        print(f"  strategy          : {strategy}")
    print(
        f"  records           : {scoreboard.get('n_records')} "
        f"(resolved={scoreboard.get('n_resolved')}, censored={scoreboard.get('n_censored')})"
    )
    if scoreboard.get("n_resolved", 0) == 0:
        print("  verdict           : " + str(scoreboard.get("verdict")))
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
        default=_DEFAULT_CORPUS,
        help=f"Path to labeled_corpus.jsonl (default: {_DEFAULT_CORPUS}).",
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
        choices=(STRATEGY_LAUNCH, STRATEGY_MOMENTUM),
        help=(
            f"Selection strategy: {STRATEGY_LAUNCH!r} (decide at t0, default) or "
            f"{STRATEGY_MOMENTUM!r} (decide at --entry-horizon seconds from the early "
            "price/pressure trajectory, then hold)."
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

    resolver = _default_resolver(args.rpc_url)
    # PERF: concurrently prefetch + disk-cache the corpus's block_times (identical values &
    # censoring as the sequential path). A no-RPC fail-closed resolver is passed through as-is.
    resolver = _maybe_prefetch_resolver(
        resolver,
        args.corpus,
        cache_path=(args.blocktime_cache or None),
        max_in_flight=args.resolve_concurrency,
    )
    exit_code, scoreboard = run_edge_proof(
        args.corpus,
        block_time_resolver=resolver,
        out_path=args.out,
        strategy=args.strategy,
        entry_horizon_s=args.entry_horizon,
        exit_model=args.exit_model,
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
