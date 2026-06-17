"""M1 Sensors — ingestion transport, decoders, message bus, point-in-time store.

T-300: data-ingestion-engineer

Implemented components:
  - registry.py      ProgramRegistry (pluggable program-ID registry, no hot-path literals)
  - decoders.py      pump.fun / PumpSwap / Raydium v4 / CPMM instruction decoders
  - transport.py     TransportInterface ABC + GeyserTransport stub + ReplayTransport
  - bus.py           Redis Streams BusProducer / BusConsumer (XADD / XREADGROUP + XACK)
  - store.py         PointInTimeStoreWriter + ShadowRecorder (event-time correct)

Ownership seams (charter M1 / BLUEPRINT.md §6):
  - This module publishes raw decoded events and raw series ONLY.
  - All feature MATH belongs to feature-quant-engineer (T-304/T-305).
  - All sentiment scoring belongs to nlp-sentiment-engineer (T-306).
  - No execution, no signing, no transaction submit.

Hard rules enforced structurally:
  - NO program-ID literals in decoders (all IDs via ProgramRegistry from config).
  - NO float money fields (all SOL/token amounts are int lamports/base-units).
  - NO truth_* fields, NO win_rate fields, NO labels/ writes.
  - recorded_at_ms >= event_time.block_time_ms (ProvenanceTaintError on violation).
  - DRY_RUN_ENABLED=true is the boot default; no real capital here.
  - LLM never on the FAST/SNIPE path (not present in this module).
"""

from aats.ingestion.bus import (
    GROUP_SLOW,
    GROUP_SNIPE,
    MAXLEN_LAUNCH_EVENTS,
    STREAM_LAUNCH_EVENTS,
    BusConsumer,
    BusMessage,
    BusProducer,
)
from aats.ingestion.completeness import (
    AuditReport,
    CensusRecord,
    CensusSource,
    CompletenessAuditor,
    CompletenessRow,
    CompletenessStatus,
    InMemoryCensusSource,
)
from aats.ingestion.decoders import (
    InstructionRouter,
    PumpFunDecoder,
    PumpSwapDecoder,
    RawInstruction,
    RawTransaction,
    RaydiumCpmmDecoder,
    RaydiumV4Decoder,
)
from aats.ingestion.registry import ProgramRegistry, VenueEntry
from aats.ingestion.store import (
    InMemoryParquetBackend,
    PointInTimeRecord,
    PointInTimeStoreWriter,
    ShadowRecorder,
    ShadowSnapshot,
)
from aats.ingestion.transport import (
    EnhancedWsFallback,
    GeyserTransport,
    ReplayTransport,
    TransportInterface,
    TransportPipeline,
    TransportStats,
)

__all__ = [
    # completeness audit (T-303, C-6)
    "AuditReport",
    "CensusRecord",
    "CensusSource",
    "CompletenessAuditor",
    "CompletenessRow",
    "CompletenessStatus",
    "InMemoryCensusSource",
    # registry
    "ProgramRegistry",
    "VenueEntry",
    # decoders
    "InstructionRouter",
    "PumpFunDecoder",
    "PumpSwapDecoder",
    "RaydiumCpmmDecoder",
    "RaydiumV4Decoder",
    "RawInstruction",
    "RawTransaction",
    # transport
    "EnhancedWsFallback",
    "GeyserTransport",
    "ReplayTransport",
    "TransportInterface",
    "TransportPipeline",
    "TransportStats",
    # bus
    "GROUP_SLOW",
    "GROUP_SNIPE",
    "MAXLEN_LAUNCH_EVENTS",
    "STREAM_LAUNCH_EVENTS",
    "BusConsumer",
    "BusMessage",
    "BusProducer",
    # store
    "InMemoryParquetBackend",
    "PointInTimeRecord",
    "PointInTimeStoreWriter",
    "ShadowRecorder",
    "ShadowSnapshot",
]
