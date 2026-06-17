"""M3 Controller — frozen control-plane API server (T-341).

Implements EVERY endpoint in api-contracts.md (FROZEN, T-201) exactly.
No endpoint, field, or enum value deviates from that contract.

Key constraints enforced in code (not comments):
  - GET = read-only. POST = de-risk-only.
  - Money on the wire: integer lamports or decimal-string, NEVER float.
  - /api/feed is SSE from the ops.feed queue; lag <= 3s (FR-056, AC-047).
  - /api/mode LIVE requires DRY_RUN_ENABLED=false AND CEO auth token (AC-060).
  - No win-rate field anywhere in any response (HONESTY CLAUSE, AC-037).
  - LLM / slow-model NEVER on the FAST/SNIPE critical path.
  - Every operator command may only DE-RISK.
  - Asymmetric trust: risk-increasing commands rejected at the contract layer.
"""

from aats.control_plane.server import ControlPlaneConfig, FeedBus, build_app

__all__ = ["build_app", "ControlPlaneConfig", "FeedBus"]
