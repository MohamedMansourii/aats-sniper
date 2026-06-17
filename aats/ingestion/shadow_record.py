"""M1 shadow/record entrypoint — first-K-slot data collection for SHADOW mode.

OPERATOR QUICK-START
--------------------
DEMONSTRATION (offline, no network required):
    python -m aats.ingestion.shadow_record \\
        --source=replay \\
        --out /tmp/aats_shadow_demo \\
        --max-events 25

REAL DATA COLLECTION (requires a deployed node — see OPERATOR NOTES below):
    python -m aats.ingestion.shadow_record \\
        --source=geyser \\
        --out /data/aats_shadow \\
        --max-events 5000

HONESTY NOTICE
--------------
``--source=replay`` produces a DEMONSTRATION corpus from SYNTHETIC launch
transactions derived from the same fixture set used by the decoder unit tests.
The transactions are DETERMINISTIC and DO NOT represent real on-chain activity.
They prove that the decode / record pipeline is wired correctly, but they carry
NO edge signal and MUST NOT be used to derive win-rates or back-test statistics.

REAL data collection requires ``--source=geyser`` with:
  - A running Geyser/Yellowstone gRPC endpoint (GEYSER_ENDPOINT env var)
  - A valid auth token (GEYSER_TOKEN env var)
  - Connection to Solana mainnet or a mainnet fork

This is the operator's R3-path action (EDGE-VERDICT.md Block A).  The recorded
corpus from real data is the prerequisite for training and edge validation.

HARD RULES (enforced by construction)
--------------------------------------
- DRY RUN ALWAYS: this module NEVER submits transactions, holds a keypair,
  or touches the OMS.  It is read-side only.
- No float money: sol_reserve_lamports and token_reserve_base are always int.
- No win-rate: no truth_* field, no profit/loss field, no label write.
- No secrets in code: all credentials come from environment variables.
- event_time is the ONLY authoritative clock.  recorded_at is wall-clock for
  monitoring only and is ALWAYS >= event_time.block_time_ms (honesty enforced
  by PointInTimeStoreWriter).

OUTPUT FORMAT
-------------
The recorded corpus is written as JSON-L to <out>/snapshots.jsonl.
Each line is one ShadowSnapshot row (see store.py for the schema).
The file is readable back with json.loads() for immediate inspection.

  recorded_at_ms  — wall-clock when the snapshot was flushed (monitoring only)
  event_slot      — on-chain slot of the first event for the mint
  event_block_time_ms — authoritative on-chain block time (event_time anchor)
  completeness_status — "complete" | "CENSORED"
  event_count     — number of raw LaunchEvents captured in the K-slot window

OPERATOR NOTES
--------------
- The Geyser transport requires GEYSER_ENDPOINT and GEYSER_TOKEN env vars.
  Without them the transport logs a clear error and yields nothing safely.
- The program-allowlist path defaults to config/program-allowlist.json relative
  to the project root.  Override with --allowlist.
- No data is written to the git repo.  --out defaults to a system temp dir.
- Connection health is shown as data_staleness_ms; a rising value means the
  feed is degraded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging — structured, ISO timestamps, level from LOG_LEVEL env (default INFO)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("aats.shadow_record")

# ---------------------------------------------------------------------------
# Project root detection — config lives relative to the package root
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
# aats/ingestion/shadow_record.py → ../../ = project root
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent


def _default_allowlist() -> Path:
    return _PROJECT_ROOT / "config" / "program-allowlist.json"


# ---------------------------------------------------------------------------
# Demo transaction generator
# ---------------------------------------------------------------------------

def _make_demo_transactions() -> list:
    """Generate a deterministic set of synthetic launch transactions.

    These are the same fixture transactions used by the decoder unit tests.
    They cover all four venue paths:
      - pump.fun create, buy, sell, withdraw (migration)
      - PumpSwap create_pool
      - Raydium AMM v4 initialize2
      - Raydium CPMM initialize + swap

    The slot and block_time values are real-looking but SYNTHETIC.
    This function imports from the test fixtures, which live under tests/
    and are importable at runtime.
    """
    # Standard library imports needed for inline buy fixture construction.
    import base64
    import hashlib
    import struct

    from aats.ingestion.decoders import RawInstruction, RawTransaction

    # Import test fixtures — they are deterministic and require no network.
    # We do this lazily so the import error is clear if the path is wrong.
    try:
        from tests.ingestion.fixtures import (
            make_pumpfun_create_tx,
            make_pumpfun_sell_tx,
            make_pumpfun_withdraw_tx,
            make_pumpswap_create_pool_tx,
            make_raydium_cpmm_init_tx,
            make_raydium_cpmm_swap_tx,
            make_raydium_v4_init2_tx,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Could not import test fixtures for demo mode.  "
            "Run from the project root: python -m aats.ingestion.shadow_record.  "
            f"Original error: {exc}"
        ) from exc

    def _disc(name: str) -> bytes:
        return hashlib.sha256(f"global:{name}".encode()).digest()[:8]

    # Base block_time: anchored to make_pumpfun_create_tx (slot=300_000_000,
    # block_time_unix_s=1_718_700_000) so that inline buy slots and block_times
    # are strictly monotonically increasing relative to the create event.
    # Using a different origin would make block_time decrease as slot increases —
    # a non-monotonic sequence unlike real chain data.
    BASE_SLOT = 300_000_000
    BASE_TIME = 1_718_700_000  # must match make_pumpfun_create_tx block_time_unix_s
    # pump.fun program ID — loaded from the allowlist by the registry.
    # It appears here ONLY as a fixture value for the demo; decoders never
    # hard-code it (execution-venue.md §3.2 build guard).
    PUMP_PID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

    # We create multiple "launches" to exercise the K-slot window accumulator.
    # Launch A: pump.fun bonding curve lifecycle (create → buy × N → sell → withdraw)
    # Launch B: PumpSwap pool creation (post-migration)
    # Launch C: Raydium v4 initialize2
    # Launch D: Raydium CPMM initialize + swap

    txs = []

    # -- Launch A: pump.fun lifecycle --
    # Create (slot 300_000_000, block_time 1_718_700_000 from fixture)
    txs.append(make_pumpfun_create_tx())

    # Multiple buys on the same mint across consecutive synthetic slots
    for i in range(5):
        token_amount = 1_000_000_000 + i * 100_000_000
        sol_cost = 50_000_000 + i * 10_000_000
        ix_data = _disc("buy") + struct.pack("<Q", token_amount) + struct.pack("<Q", sol_cost)
        ix = RawInstruction(
            program_id=PUMP_PID,
            data_b64=base64.b64encode(ix_data).decode(),
            account_keys=[
                "GlobalState111111111111111111111111111111111",
                "FeeRecipient1111111111111111111111111111111111",
                "MintPumpFun1111111111111111111111111111111111",
                "BondingCurve11111111111111111111111111111111",
                "AssocBondingCrv11111111111111111111111111111",
                f"BuyerATA{i:02d}11111111111111111111111111111111111",
                f"BuyerWallet{i:02d}111111111111111111111111111111111",
            ],
            program_index=0,
        )
        txs.append(RawTransaction(
            signature=f"pumpfun_buy_demo_{i:04d}",
            slot=BASE_SLOT + 1 + i,
            block_time_unix_s=BASE_TIME + 1 + i,
            fee_payer=f"BuyerWallet{i:02d}111111111111111111111111111111111",
            instructions=[ix],
            inner_instructions=[],
            program_logs=[],
        ))

    # Sell event
    txs.append(make_pumpfun_sell_tx())

    # Withdraw = migration trigger (highest-value signal)
    txs.append(make_pumpfun_withdraw_tx())

    # -- Launch B: PumpSwap (post-migration AMM) --
    txs.append(make_pumpswap_create_pool_tx())

    # -- Launch C: Raydium v4 --
    txs.append(make_raydium_v4_init2_tx())

    # -- Launch D: Raydium CPMM init + swap --
    txs.append(make_raydium_cpmm_init_tx())
    txs.append(make_raydium_cpmm_swap_tx())

    return txs


# ---------------------------------------------------------------------------
# Corpus writer — JSON-L, not inside the repo
# ---------------------------------------------------------------------------

class CorpusWriter:
    """Writes ShadowSnapshot rows as JSON-L to <out_dir>/snapshots.jsonl.

    JSON-L: one JSON object per line, append-mode.  Readable with any
    standard JSON parser:  json.loads(line) for line in open(path)

    Does NOT write Parquet in this demo entrypoint (no pyarrow dependency
    required).  The InMemoryParquetBackend holds all rows in memory;
    this writer serializes them on flush.
    """

    def __init__(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self._path = out_dir / "snapshots.jsonl"
        self._fh = self._path.open("w", encoding="utf-8")
        self._rows_written = 0

    def write_row(self, row: dict) -> None:
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()
        self._rows_written += 1

    def close(self) -> None:
        self._fh.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def rows_written(self) -> int:
        return self._rows_written


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run(
    source: str,
    out_dir: Path,
    max_events: int,
    allowlist_path: Path,
    first_k_slots: int,
) -> dict:
    """Assemble and drive the ingestion pipeline.

    Returns a stats dict for the CLI summary.
    """
    from aats.ingestion.decoders import InstructionRouter
    from aats.ingestion.registry import ProgramRegistry
    from aats.ingestion.store import InMemoryParquetBackend, PointInTimeStoreWriter, ShadowRecorder
    from aats.ingestion.transport import (
        EnhancedWsFallback,
        GeyserTransport,
        ReplayTransport,
        TransportPipeline,
    )

    # -- ProgramRegistry (offline — no live verification in demo mode) --
    registry = ProgramRegistry.from_allowlist(
        allowlist_path,
        verifier=None,       # offline: no getAccountInfo call
        filter_active_only=True,
    )
    logger.info("Registry loaded: %r", registry)

    # -- Decoder router --
    router = InstructionRouter(registry)

    # -- Storage backend (in-memory; CorpusWriter serializes to disk) --
    backend = InMemoryParquetBackend()
    store_writer = PointInTimeStoreWriter(
        redis_client=None,   # no Redis in demo mode
        parquet_backend=backend,
    )
    shadow_recorder = ShadowRecorder(
        parquet_backend=backend,
        first_k_slots=first_k_slots,
        max_open_windows=500,
    )
    corpus = CorpusWriter(out_dir)

    # -- Transport selection --
    if source == "replay":
        logger.info(
            "SOURCE=replay — DEMONSTRATION mode.  "
            "Generating synthetic launch transactions from test fixtures.  "
            "This is NOT real on-chain data."
        )
        demo_txs = _make_demo_transactions()
        transport = ReplayTransport(
            transactions=demo_txs,
            tick_ms=0.0,  # as fast as possible (no artificial delay)
        )
    elif source == "geyser":
        # Real Geyser — credentials come from env, never hardcoded.
        # GEYSER_TOKEN is the canonical name (see .env.example).
        # GEYSER_X_TOKEN is accepted as a transition fallback only.
        endpoint = os.environ.get("GEYSER_ENDPOINT", "")
        x_token = os.environ.get("GEYSER_TOKEN") or os.environ.get("GEYSER_X_TOKEN", "")
        shredstream = os.environ.get("SHREDSTREAM_ENDPOINT") or None
        if not endpoint:
            logger.warning(
                "GEYSER_ENDPOINT is not set.  "
                "GeyserTransport will log a clear error and yield no events.  "
                "Set GEYSER_ENDPOINT + GEYSER_TOKEN (see .env.example) to "
                "connect to a live Yellowstone/Helius/Triton endpoint."
            )
        transport = GeyserTransport(
            endpoint=endpoint,
            x_token=x_token,
            shredstream_endpoint=shredstream,
        )
    elif source == "ws":
        # Free-tier WS transport — standard Solana logsSubscribe + RPC polling fallback.
        # Credentials come from environment ONLY, never hardcoded.
        #
        # WS URL precedence:
        #   1. WS_ENDPOINT env var (explicit)
        #   2. Derive from RPC_PRIMARY: replace https:// with wss://
        # RPC URL: always RPC_PRIMARY (used for getTransaction + polling)
        rpc_url = os.environ.get("RPC_PRIMARY", "")
        ws_url = os.environ.get("WS_ENDPOINT", "")
        if not ws_url and rpc_url:
            # Derive WS URL from RPC_PRIMARY (https→wss, http→ws)
            ws_url = rpc_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

        if not rpc_url:
            logger.error(
                "SOURCE=ws requires RPC_PRIMARY to be set in the environment.  "
                "Example: RPC_PRIMARY=https://mainnet.helius-rpc.com/?api-key=<key>  "
                "See .env.example for the full schema.  "
                "Yielding nothing — set RPC_PRIMARY to use --source=ws."
            )
            # Build the transport anyway so the pipeline wires up cleanly;
            # EnhancedWsFallback will log the error and yield nothing.

        logger.info(
            "SOURCE=ws — standard Solana WS + RPC fallback transport.  "
            "WS endpoint configured: %s  RPC configured: %s",
            "YES" if ws_url else "NO (polling-only mode)",
            "YES" if rpc_url else "NO (will error)",
        )
        transport = EnhancedWsFallback(
            ws_url=ws_url,
            rpc_url=rpc_url,
        )
    else:
        raise ValueError(
            f"Unknown --source value: {source!r}.  Choose 'replay', 'geyser', or 'ws'."
        )

    pipeline = TransportPipeline(transport=transport, router=router)

    # -- Drive the pipeline --
    events_decoded = 0
    start_wall_ms = int(time.time() * 1_000)

    logger.info(
        "Starting ingestion: source=%s out=%s max_events=%d first_k_slots=%d",
        source, out_dir, max_events, first_k_slots,
    )

    async for event, sig, event_kind in pipeline.events(last_slot=0):
        # 1. Write to point-in-time store (provenance honesty enforced here).
        #    ALL decoded events (including buys/sells) are written to the
        #    point-in-time store for feature-quant (first-K microstructure).
        await store_writer.write_launch_event(event, sig)

        # 2. Feed shadow recorder (K-slot window accumulator).
        #    The recorder gates window-opening on event_kind:
        #      CREATE/WITHDRAW/INIT → opens a window on the first occurrence.
        #      BUY/SELL/SWAP       → attributed to an existing window; orphan
        #                            if no window exists (T-LAUNCH-FILTER).
        shadow_recorder.observe(event, event_kind)

        events_decoded += 1
        logger.debug(
            "event[%d] mint=%s source=%s kind=%s slot=%d staleness=%dms",
            events_decoded,
            event.mint,
            event.source,
            event_kind,
            event.event_time.slot,
            event.data_staleness_ms,
        )

        if events_decoded >= max_events:
            logger.info("Reached max_events=%d — stopping.", max_events)
            break

    # Flush all open snapshot windows (marks any incomplete windows CENSORED)
    shadow_recorder.flush_all(status="complete")

    # Write completed snapshots to corpus.
    # InMemoryParquetBackend.all_rows() returns to_row() dicts which carry the
    # PointInTimeRecord envelope fields (dataset, mint, event_slot,
    # event_block_time_ms, recorded_at_ms, data_staleness_ms, payload_json).
    # For shadow_snapshots the payload_json IS the full rich snapshot dict
    # (completeness_status, event_count, events_json, first_k_slots, event_date).
    # We merge the payload into the envelope so the corpus rows are flat and
    # self-describing — no secondary parse required by the reader.
    snapshots = backend.all_rows(dataset="shadow_snapshots")
    for row in snapshots:
        try:
            payload = json.loads(row.get("payload_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        # Merge: envelope fields take precedence for the canonical join keys
        # (event_slot, event_block_time_ms, recorded_at_ms, mint) so that
        # point-in-time correctness is enforced at the corpus level.
        merged = {**payload, **{k: v for k, v in row.items() if k != "payload_json"}}
        corpus.write_row(merged)
    corpus.close()

    # Compute final stats
    elapsed_ms = int(time.time() * 1_000) - start_wall_ms
    stats = {
        "source": source,
        "events_decoded": events_decoded,
        "events_skipped": pipeline.stats.events_skipped,
        "decode_errors": pipeline.stats.decode_errors,
        "snapshots_recorded": len(snapshots),
        # orphan_events: buy/sell/swap received before any matching create/init.
        # A rising orphan count means the ingest stream is seeing pre-existing
        # tokens — expected in a live run but should be 0 in the replay demo.
        "orphan_events": shadow_recorder.orphan_events_total,
        "corpus_path": str(corpus.path),
        "elapsed_ms": elapsed_ms,
        "data_staleness_ms": pipeline.stats.data_staleness_ms,
    }
    return stats


# ---------------------------------------------------------------------------
# Corpus read-back: show a sample for operator verification
# ---------------------------------------------------------------------------

def _show_corpus_sample(corpus_path: Path, n: int = 3) -> None:
    """Read back the corpus and print N sample records.

    Demonstrates that the recorded corpus is:
    - Readable as plain JSON-L
    - Correctly carries event_time fields (event_slot, event_block_time_ms)
    - Has recorded_at_ms >= event_block_time_ms (honesty invariant)
    """
    lines = corpus_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        logger.info("Corpus is empty — no snapshots to display.")
        return

    print("\n--- Corpus sample (first %d of %d snapshots) ---" % (min(n, len(lines)), len(lines)))
    for i, line in enumerate(lines[:n]):
        row = json.loads(line)
        event_block_time_ms = row.get("event_block_time_ms", 0)
        recorded_at_ms = row.get("recorded_at_ms", 0)
        honesty_ok = recorded_at_ms >= event_block_time_ms

        print(
            f"  [{i}] mint={row.get('mint','?')!r:48s}"
            f"  event_slot={row.get('event_slot','?')}"
            f"  event_block_time_ms={event_block_time_ms}"
            f"  recorded_at_ms={recorded_at_ms}"
            f"  staleness_at_flush={recorded_at_ms - event_block_time_ms}ms"
            f"  honesty_ok={honesty_ok}"
            f"  status={row.get('completeness_status','?')}"
            f"  event_count={row.get('event_count','?')}"
        )
        if not honesty_ok:
            # This is a data integrity violation — raise loudly.
            raise RuntimeError(
                f"PROVENANCE TAINT: row[{i}] has recorded_at_ms ({recorded_at_ms}) "
                f"< event_block_time_ms ({event_block_time_ms}).  "
                "This violates the point-in-time correctness invariant."
            )
    print("--- honesty invariant: ALL recorded_at_ms >= event_block_time_ms ---\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m aats.ingestion.shadow_record",
        description=(
            "AATS M1 shadow/record entrypoint — first-K-slot data collection.\n\n"
            "HONESTY: --source=replay uses SYNTHETIC fixtures (no real edge data).\n"
            "Real data requires --source=geyser with a live RPC/Geyser endpoint.\n"
            "DRY RUN ALWAYS: this module never submits transactions or touches the OMS."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=["replay", "geyser", "ws"],
        default="replay",
        help=(
            "replay = deterministic offline demo (SYNTHETIC data, default).  "
            "geyser = live Geyser gRPC (requires GEYSER_ENDPOINT + GEYSER_TOKEN env).  "
            "ws = standard Solana logsSubscribe WS + RPC polling fallback "
            "(requires RPC_PRIMARY; WS_ENDPOINT optional, derived from RPC_PRIMARY if absent). "
            "Works on a free-tier Helius RPC key — no paid gRPC needed."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output directory for the recorded corpus.  "
            "Defaults to a system temp dir (NOT inside the repo).  "
            "The corpus is written as JSON-L to <out>/snapshots.jsonl."
        ),
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=100,
        help="Stop after ingesting this many decoded events (default 100).",
    )
    parser.add_argument(
        "--first-k-slots",
        type=int,
        default=10,
        help="K: capture the first K slots of events per mint (default 10).",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Path to program-allowlist.json.  "
            "Defaults to <project_root>/config/program-allowlist.json."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Resolve output directory — never inside the repo
    if args.out is None:
        tmp = tempfile.mkdtemp(prefix="aats_shadow_")
        out_dir = Path(tmp)
    else:
        out_dir = args.out.resolve()

    allowlist_path = args.allowlist or _default_allowlist()
    if not allowlist_path.exists():
        logger.error(
            "Program allowlist not found at %s.  "
            "Run from the project root or pass --allowlist.",
            allowlist_path,
        )
        sys.exit(1)

    # Safety assertion — DRY RUN ALWAYS
    dry_run = os.environ.get("DRY_RUN_ENABLED", "true").lower()
    if dry_run not in ("true", "1", "yes"):
        # An operator explicitly set DRY_RUN_ENABLED=false.
        # This module is read-side only and never executes capital, so we can
        # proceed, but we must be loud about it so the operator notices.
        logger.warning(
            "DRY_RUN_ENABLED=%r in env but shadow_record.py is READ-SIDE ONLY "
            "and NEVER submits transactions.  Capital mode has no effect here.",
            os.environ.get("DRY_RUN_ENABLED"),
        )

    logger.info(
        "shadow_record starting: source=%s out=%s max_events=%d first_k_slots=%d allowlist=%s",
        args.source, out_dir, args.max_events, args.first_k_slots, allowlist_path,
    )
    if args.source == "replay":
        logger.info(
            "DEMONSTRATION MODE: synthetic transactions derived from test fixtures.  "
            "For real edge data use --source=geyser or --source=ws with a live node."
        )
    elif args.source == "ws":
        logger.info(
            "WS MODE: standard Solana logsSubscribe WebSocket + RPC polling fallback.  "
            "Requires RPC_PRIMARY env var (e.g. https://mainnet.helius-rpc.com/?api-key=<key>).  "
            "WS_ENDPOINT is derived from RPC_PRIMARY if not explicitly set.  "
            "DRY RUN ALWAYS: this module is READ-ONLY (no signing, no capital)."
        )

    stats = asyncio.run(
        _run(
            source=args.source,
            out_dir=out_dir,
            max_events=args.max_events,
            allowlist_path=allowlist_path,
            first_k_slots=args.first_k_slots,
        )
    )

    # Read back sample to prove corpus is correct
    corpus_path = Path(stats["corpus_path"])
    if corpus_path.exists() and corpus_path.stat().st_size > 0:
        _show_corpus_sample(corpus_path, n=3)

    # Print final stats
    print("\n=== shadow_record stats ===")
    print(f"  source            : {stats['source']}")
    print(f"  events_decoded    : {stats['events_decoded']}")
    print(f"  events_skipped    : {stats['events_skipped']}")
    print(f"  decode_errors     : {stats['decode_errors']}")
    print(f"  snapshots_recorded: {stats['snapshots_recorded']}")
    print(f"  corpus_path       : {stats['corpus_path']}")
    print(f"  elapsed_ms        : {stats['elapsed_ms']}")
    print(f"  data_staleness_ms : {stats['data_staleness_ms']}  (synthetic — staleness not meaningful on replay)")
    if args.source == "replay":
        print(
            "\n  NOTE: corpus is SYNTHETIC (demo only).  "
            "Real edge data requires --source=geyser on a deployed node."
        )
    print("===========================\n")

    logger.info("Corpus written to: %s", stats["corpus_path"])


if __name__ == "__main__":
    main()
