"""FAST-loop no-LLM / no-await static proof (charter self-check #5).

This module is touched by the 2026-07-03 catastrophic-exit wiring fix (a new
StateStore read — get_lp_unlock_approaching_flag — was added to FastLoop.tick()).
This test proves that addition (and the module as a whole) never introduces an
`await`, an LLM/reasoning import, or a call into the SLOW-loop/enrichment
producers — the FAST loop only ever reads PRE-SET booleans from the StateStore.
"""

from __future__ import annotations

import ast
import inspect

from aats.controller import fast_loop as fast_loop_module
from aats.controller.fast_loop import FastLoop

# Modules the FAST loop must NEVER import (would mean a hidden blocking call
# on the reasoner / slow model / enrichment-tier producers from the hot path).
_FORBIDDEN_IMPORT_PREFIXES = (
    "aats.reasoning",
    "aats.nlp",
    "aats.models",  # ML prediction models
    "aats.controller.slow_loop",
    "aats.controller.enrichment_wiring",
    "aats.ingestion.insider_dump",
    "aats.risk.sellability_reprobe",
    "aats.risk.lp_unlock_gate",
)


def _imported_module_names(src: str) -> set[str]:
    """Return the set of dotted module names actually IMPORTED by `src`
    (via `import x` / `from x import y`) — parsed with `ast`, so prose
    mentioning a module name in a docstring/comment is never mistaken for
    an import (unlike a naive substring search)."""
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_fast_loop_module_source_has_no_await() -> None:
    """The entire fast_loop.py module contains zero `await` expressions."""
    src = inspect.getsource(fast_loop_module)
    assert "await " not in src, (
        "fast_loop.py must contain NO 'await ' anywhere — the FAST loop is fully "
        "synchronous (BLUEPRINT §2.1)."
    )
    assert "async def" not in src, "fast_loop.py must define no async functions."


def test_fast_loop_tick_source_has_no_await() -> None:
    """FastLoop.tick() specifically — the hot-path method — has no await."""
    src = inspect.getsource(FastLoop.tick)
    assert "await " not in src


def test_fast_loop_module_imports_no_llm_or_enrichment_producer_modules() -> None:
    """fast_loop.py must NEVER import the reasoner, the slow model, or any of
    the E14/E17/E19 enrichment-tier PRODUCER modules — it only ever reads
    pre-set booleans (get_insider_dump_flag / get_sellability_degraded_flag /
    get_lp_unlock_approaching_flag / get_narrative_failure_flag) from the
    StateStore.  A hung LLM / slow reprobe / schedule decode must NEVER be able
    to stall a stop trigger."""
    src = inspect.getsource(fast_loop_module)
    imported = _imported_module_names(src)
    for forbidden_prefix in _FORBIDDEN_IMPORT_PREFIXES:
        matches = [m for m in imported if m == forbidden_prefix or m.startswith(forbidden_prefix + ".")]
        assert not matches, (
            f"fast_loop.py must not IMPORT anything under {forbidden_prefix!r} "
            f"(found: {matches}) — that would put a slow/enrichment-tier "
            "producer on the FAST-loop critical path."
        )


def test_fast_loop_reads_all_four_pre_set_flags_as_bare_bools() -> None:
    """FastLoop.tick() reads all four catastrophic-exit flags as bare booleans
    from the StateStore (never computing them) and forwards them unchanged
    into ExitEngine.on_tick() — proven by source inspection so a regression
    that silently drops one of the four flags is caught here, not only by the
    (probabilistic) E2E behavioural tests."""
    src = inspect.getsource(FastLoop.tick)
    for flag_getter in (
        "get_narrative_failure_flag",
        "get_insider_dump_flag",
        "get_sellability_degraded_flag",
        "get_lp_unlock_approaching_flag",
    ):
        assert flag_getter in src, f"FastLoop.tick() must call store.{flag_getter}(mint)"
    for kw in (
        "narrative_failure_flag=",
        "insider_dump_flag=",
        "sellability_degraded_flag=",
        "lp_unlock_approaching_flag=",
    ):
        assert kw in src, f"FastLoop.tick() must forward {kw} into ExitEngine.on_tick()"
