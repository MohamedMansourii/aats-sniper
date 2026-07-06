"""Tests for `python -m aats.backtest.run_edge_proof` — the runnable edge proof.

Self-check criteria:
  1. FAIL-CLOSED: an empty corpus (or an unresolvable one) yields NO metric and a non-zero
     exit — never a fabricated pass. This is exactly the state the earlier proof was in.
  2. VERDICT: a fixture corpus + fixture block_time resolver produces a GATE-A / GATE-B
     scoreboard and a GO / NO-GO verdict.
  3. WRITES: `--out` writes the scoreboard + TradeOutcome JSON (outside the repo).

Offline / injectable / deterministic. No RPC, no network, no keypair, no capital.
"""

from __future__ import annotations

import json

from aats.backtest.outcome_harness import BlockTime, FixtureBlockTimeResolver
from aats.backtest.run_edge_proof import (
    EXIT_FAIL_CLOSED,
    EXIT_GO,
    EXIT_NO_GO,
    run_edge_proof,
)


def _write_corpus(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _rec_dict(mint, sig, *, sol_amount, v_sol_sol, forward_multiples, v_tokens="1000000000"):
    from decimal import Decimal

    entry_price = Decimal(v_sol_sol) / Decimal(v_tokens)
    forward = []
    for horizon, mult in zip((60, 300, 900), forward_multiples, strict=True):
        price = None if mult is None else str(entry_price * Decimal(mult))
        forward.append(
            {"price_sol": price, "liquidity_usd": None, "dex": "pumpfun", "note": "ok",
             "horizon_s": horizon, "obs_wall_ms": 1_700_000_000_000 + horizon * 1000}
        )
    entry = {
        "mint": mint, "signature": sig, "traderPublicKey": "T", "initialBuy": 1_000_000.0,
        "solAmount": float(sol_amount), "vTokensInBondingCurve": float(v_tokens),
        "vSolInBondingCurve": float(v_sol_sol), "marketCapSol": float(v_sol_sol),
        "name": "n", "symbol": "s", "pool": "pump", "recv_wall_ms": 1_700_000_000_000,
    }
    return {"entry": entry, "forward": forward}


def _resolver(records: list[dict]) -> FixtureBlockTimeResolver:
    return FixtureBlockTimeResolver(
        {
            r["entry"]["signature"]: BlockTime(
                slot=300_000_000 + i, block_time_ms=1_700_000_100_000 + i
            )
            for i, r in enumerate(records)
        }
    )


def test_fail_closed_on_empty_corpus(tmp_path) -> None:
    corpus = tmp_path / "empty.jsonl"
    corpus.write_text("", encoding="utf-8")
    code, scoreboard = run_edge_proof(corpus, block_time_resolver=FixtureBlockTimeResolver({}))
    assert code == EXIT_FAIL_CLOSED
    assert scoreboard["n_resolved"] == 0
    assert "FAIL-CLOSED" in scoreboard["verdict"]


def test_fail_closed_when_no_block_time_resolves(tmp_path) -> None:
    records = [
        _rec_dict("M1", "s1", sol_amount="0.06", v_sol_sol="32", forward_multiples=["1", "2", "3"])
    ]
    corpus = tmp_path / "c.jsonl"
    _write_corpus(corpus, records)
    # Resolver has no mapping -> every record CENSORED -> fail-closed (no fabricated anchor).
    code, scoreboard = run_edge_proof(corpus, block_time_resolver=FixtureBlockTimeResolver({}))
    assert code == EXIT_FAIL_CLOSED
    assert scoreboard["n_records"] == 1
    assert scoreboard["n_resolved"] == 0
    assert scoreboard["n_censored"] == 1


def test_verdict_go_on_winner_heavy_corpus(tmp_path) -> None:
    # 12 model-winners (3x, high liquidity, tiny dev buy) + 12 baseline-losers (flat, big
    # dev buy). Model cohort = winners, baseline cohort = losers -> positive edge.
    records = [
        _rec_dict(f"WIN{i}", f"win{i}", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "2", "3"])
        for i in range(12)
    ] + [
        _rec_dict(f"LOSE{i}", f"lose{i}", sol_amount="0.5", v_sol_sol="30",
                  forward_multiples=["1", "1", "1"])
        for i in range(12)
    ]
    corpus = tmp_path / "winners.jsonl"
    _write_corpus(corpus, records)
    code, scoreboard = run_edge_proof(corpus, block_time_resolver=_resolver(records))
    assert scoreboard["n_resolved"] == 24
    assert scoreboard["n_model_selected"] == 12
    assert scoreboard["n_baseline_selected"] == 12
    assert scoreboard["gate_a_model_pass"] is True
    assert code in (EXIT_GO, EXIT_NO_GO)
    assert scoreboard["verdict"] in ("GO", "NO-GO")


def test_no_go_on_losing_corpus(tmp_path) -> None:
    # Model selects flat losers (v_sol high, forward flat) -> negative net -> NO-GO.
    records = [
        _rec_dict(f"FLAT{i}", f"flat{i}", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "1", "1"])
        for i in range(12)
    ]
    corpus = tmp_path / "losers.jsonl"
    _write_corpus(corpus, records)
    code, scoreboard = run_edge_proof(corpus, block_time_resolver=_resolver(records))
    assert scoreboard["verdict"] == "NO-GO"
    assert code == EXIT_NO_GO
    assert scoreboard["gate_a_model_pass"] is False


def test_out_writes_scoreboard_and_outcomes(tmp_path) -> None:
    records = [
        _rec_dict("M1", "s1", sol_amount="0.06", v_sol_sol="32", forward_multiples=["1", "2", "3"]),
        _rec_dict("M2", "s2", sol_amount="0.5", v_sol_sol="30", forward_multiples=["1", None, None]),
    ]
    corpus = tmp_path / "c.jsonl"
    _write_corpus(corpus, records)
    out = tmp_path / "sub" / "proof.json"
    code, scoreboard = run_edge_proof(corpus, block_time_resolver=_resolver(records), out_path=out)
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scoreboard"]["n_resolved"] == 2
    assert len(payload["outcomes"]) == 2
    for o in payload["outcomes"]:
        assert isinstance(o["net_pnl_lamports"], int)
        assert o["sol_at_risk_lamports"] > 0
    assert code in (EXIT_GO, EXIT_NO_GO)
