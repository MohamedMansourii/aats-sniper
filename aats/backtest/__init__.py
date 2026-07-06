"""Backtest / clean-room outcome-labeling lane (offline / validation only).

This package NEVER touches capital, never builds/signs/lands a transaction, and holds
no keypair.  It reads recorded launches + forward observations and produces the
`aats.models.gate_b.TradeOutcome` records the edge proof (GATE-A / GATE-B) consumes.
"""
