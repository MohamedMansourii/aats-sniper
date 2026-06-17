"""`aats.dms` — the runnable aats-dms service package (T-322).

The `aats-dms` compose unit runs `python -m aats.dms` (docker/Dockerfile.dms).
The implementation lives in `aats.risk.dms_main` (the watchdog, holder, and
service wiring) and in `aats.risk.dms_service` / `aats.risk.presigned_flatten`
(the seams).  This package is the thin process entrypoint so the deploy topology
in docker-compose.yml (`python -m aats.dms`) and infrastructure.md §8 stay exact.

The DMS is a SEPARATE FAILURE DOMAIN: it imports no decoders, opens no inbound
network, shares no memory with the FAST loop, and cannot be disarmed by an LLM,
a market event, or a risk signal — only by a fresh heartbeat or an operator
stand-down token (AC-046).
"""

from aats.risk.dms_main import DmsService, DmsServiceConfig, main

__all__ = ["DmsService", "DmsServiceConfig", "main"]
