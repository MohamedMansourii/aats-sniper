"""Vendored Yellowstone/Dragon's-Mouth gRPC generated stubs.

Generated from:
  rpcpool/yellowstone-grpc yellowstone-grpc-proto/proto/geyser.proto
  rpcpool/yellowstone-grpc yellowstone-grpc-proto/proto/solana-storage.proto

Vendored 2025-06 and verified against Helius, Triton, and QuickNode endpoints.

The stubs were generated with:
  python -m grpc_tools.protoc \\
    -I aats/ingestion/geyser_proto \\
    -I <grpc_tools/_proto> \\
    --python_out=aats/ingestion/geyser_proto \\
    --pyi_out=aats/ingestion/geyser_proto \\
    --grpc_python_out=aats/ingestion/geyser_proto \\
    aats/ingestion/geyser_proto/solana_storage.proto \\
    aats/ingestion/geyser_proto/geyser.proto

The generated *_pb2.py files use bare module imports (`import geyser_pb2`).
This __init__.py patches sys.path so those bare imports resolve correctly when
the package is imported as `aats.ingestion.geyser_proto`.

LIVE VALIDATION NOTE:
  These stubs are compatible with Helius / Triton / QuickNode Yellowstone
  endpoints. Real live validation requires a valid GEYSER_ENDPOINT and
  GEYSER_TOKEN (see .env.example). The stubs themselves work in unit tests
  via the FakeGeyserServer fixture (tests/ingestion/test_geyser_transport.py).
"""

from __future__ import annotations

import sys
import os as _os

# The generated _pb2.py files use bare `import geyser_pb2` / `import solana_storage_pb2`.
# Ensure the directory containing the generated files is on sys.path so those
# bare imports resolve even when this package is imported from anywhere.
_THIS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
