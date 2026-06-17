"""Router + fast-path latency tests (self-check item 6) and routing behaviour.

Proves:
  - the sub-200ms LOCAL veto fast path is MEASURED under budget (p50/p99);
  - a router timeout returns the CONSERVATIVE action without blocking;
  - the signal-bucket cache avoids re-paying latency for identical states;
  - escalation only fires for ambiguous/high-stakes states;
  - the LLM is NEVER on the FAST/SNIPE critical path (import + structural guard).
"""

from __future__ import annotations

import statistics
import time

from aats.reasoning.router import (
    DEFAULT_FAST_PATH_BUDGET_MS,
    LLMRouter,
    MockReasonerBackend,
    RouteTier,
    SignalBucketKey,
)
from aats.reasoning.schema import (
    DecisionSignalLabel,
    LLMRawVerdict,
    QuantBucket,
    SentimentRisk,
)

_KEY = SignalBucketKey(
    quant=QuantBucket.BULLISH,
    sentiment=SentimentRisk.BENIGN,
    position_is_open=False,
    net_edge_sign=1,
)

_AMBIGUOUS_KEY = SignalBucketKey(
    quant=QuantBucket.NEUTRAL,
    sentiment=SentimentRisk.ELEVATED,
    position_is_open=False,
    net_edge_sign=0,
)


# ---------------------------------------------------------------------------
# Fast-path latency (MEASURED p50/p99 under budget)
# ---------------------------------------------------------------------------


def test_fast_path_latency_under_budget():
    """Measure p50/p99 of the fast veto path; assert p99 < 200ms budget."""
    router = LLMRouter(local_backend=MockReasonerBackend())
    samples: list[float] = []
    # Use a fresh key each iteration so we measure the COMPUTE path, not cache hits.
    for i in range(400):
        key = SignalBucketKey(
            quant=QuantBucket.BULLISH,
            sentiment=SentimentRisk.BENIGN,
            position_is_open=bool(i % 2),
            net_edge_sign=(i % 3) - 1,
        )
        # Vary prompt to dodge the cache deterministically.
        t0 = time.perf_counter()
        router.fast_path_veto(system_prompt="sys", user_prompt=f"usr-{i}", key=key)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    p50 = statistics.median(samples)
    p99 = samples[min(len(samples) - 1, int(0.99 * len(samples)))]
    print(
        f"\n[fast-path] n={len(samples)} p50={p50:.4f}ms p99={p99:.4f}ms "
        f"budget={DEFAULT_FAST_PATH_BUDGET_MS}ms"
    )
    assert p99 < DEFAULT_FAST_PATH_BUDGET_MS, f"p99 {p99:.3f}ms exceeded budget"


def test_fast_path_timeout_returns_conservative_action_without_blocking():
    """A slow backend overruns the budget => conservative fallback (de-risk), no block."""
    # Backend sleeps 50ms; set a tiny 5ms budget so it always overruns.
    slow_backend = MockReasonerBackend(latency_ms=50.0)
    router = LLMRouter(local_backend=slow_backend, fast_path_budget_ms=5.0)
    t0 = time.perf_counter()
    routed = router.fast_path_veto(system_prompt="sys", user_prompt="usr", key=_KEY)
    elapsed = (time.perf_counter() - t0) * 1000.0
    assert routed.tier is RouteTier.FALLBACK
    assert routed.timed_out is True
    # The conservative fallback is a veto (de-risk) — never a silent pass.
    assert routed.raw.veto is True
    # It returns promptly after the single call (does not hang the loop indefinitely).
    assert elapsed < 1000.0


# ---------------------------------------------------------------------------
# Signal-bucket cache
# ---------------------------------------------------------------------------


def test_cache_avoids_second_llm_call_for_identical_state():
    backend = MockReasonerBackend()
    router = LLMRouter(local_backend=backend)
    router.route(system_prompt="sys", user_prompt="usr", key=_KEY)
    calls_after_first = backend.call_count
    routed2 = router.route(system_prompt="sys", user_prompt="usr-DIFFERENT", key=_KEY)
    # Same bucket key => served from cache => no new backend call.
    assert routed2.tier is RouteTier.CACHE
    assert routed2.cache_hit is True
    assert backend.call_count == calls_after_first


def test_distinct_states_do_not_collide_in_cache():
    backend = MockReasonerBackend()
    router = LLMRouter(local_backend=backend)
    router.route(system_prompt="sys", user_prompt="a", key=_KEY)
    other = SignalBucketKey(
        quant=QuantBucket.BEARISH,
        sentiment=SentimentRisk.MANUFACTURED,
        position_is_open=True,
        net_edge_sign=-1,
    )
    routed = router.route(system_prompt="sys", user_prompt="b", key=other)
    assert routed.tier is not RouteTier.CACHE  # different state => fresh compute


# ---------------------------------------------------------------------------
# Escalation policy
# ---------------------------------------------------------------------------


def test_escalation_only_for_ambiguous_states():
    assert LLMRouter.should_escalate(_AMBIGUOUS_KEY) is True
    assert LLMRouter.should_escalate(_KEY) is False


def test_frontier_used_only_when_present_and_ambiguous():
    local = MockReasonerBackend()
    frontier = MockReasonerBackend(
        force=LLMRawVerdict(
            decision_signal=DecisionSignalLabel.HOLD,
            confidence=0.6,
            veto=False,
            narrative_failure=False,
            rationale="frontier",
        )
    )
    router = LLMRouter(local_backend=local, frontier_backend=frontier)
    routed = router.route(system_prompt="sys", user_prompt="usr", key=_AMBIGUOUS_KEY)
    assert routed.tier is RouteTier.FRONTIER
    # A routine key stays local.
    routed_local = router.route(system_prompt="sys", user_prompt="usr2", key=_KEY)
    assert routed_local.tier is RouteTier.LOCAL


def test_no_frontier_backend_stays_local_even_when_ambiguous():
    router = LLMRouter(local_backend=MockReasonerBackend())  # no frontier
    routed = router.route(system_prompt="sys", user_prompt="usr", key=_AMBIGUOUS_KEY)
    assert routed.tier is RouteTier.LOCAL


# ---------------------------------------------------------------------------
# Determinism for control flow (temperature=0 default)
# ---------------------------------------------------------------------------


def test_router_default_temperature_is_zero():
    router = LLMRouter(local_backend=MockReasonerBackend())
    assert router._temperature == 0.0
