"""aats.sentiment — MCS adversarial sentiment pipeline (T-306).

SLOW-LOOP ONLY.  This package is FORBIDDEN on the SNIPE / FAST hot path.

Architecture (BLUEPRINT.md §1, data-models.md §5):
  Tier-A: cheap CPU/GPU filter (relevance, dedup, bot/age scoring)
  Tier-B: batched-per-asset LLM narrative scorer with coordinated-shill penalty

Output: MCSScore (aats.contracts.models) + MCSEvidence (aats.sentiment.models)

Key invariants:
  - Manufactured euphoria LOWERS conviction.
  - Bot/age weighting only reduces influence (never increases it).
  - MCS conviction may only de-risk entries; it cannot size up or widen stops.
  - Ingested social text is QUOTED UNTRUSTED DATA — never executed as instructions.
  - Point-in-time: only posts with event_time_ms <= decision_time_ms are scored.
  - ONE LLM call per asset per cadence (never per post).

Public surface:
  MCSSentimentPipeline   — the main async pipeline class
  MockSocialAdapter      — offline adapter for tests
  MockLLMBackend         — offline LLM for tests
  mcs_may_only_de_risk   — the directionality gate callable
  assert_mcs_cannot_size_up — proof callable (size is unchanged by MCS)
"""

from aats.sentiment.adapters import MockSocialAdapter, SocialAdapter
from aats.sentiment.models import MCSEvidence, PostCluster, RawPost, TierADigest
from aats.sentiment.pipeline import (
    MCSSentimentPipeline,
    assert_mcs_cannot_size_up,
    mcs_may_only_de_risk,
)
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import LLMBackend, MockLLMBackend, TierBScorer

__all__ = [
    # Pipeline
    "MCSSentimentPipeline",
    "mcs_may_only_de_risk",
    "assert_mcs_cannot_size_up",
    # Tier-A
    "run_tier_a",
    # Tier-B
    "TierBScorer",
    "LLMBackend",
    "MockLLMBackend",
    # Adapters
    "SocialAdapter",
    "MockSocialAdapter",
    # Internal models
    "RawPost",
    "PostCluster",
    "TierADigest",
    "MCSEvidence",
]
