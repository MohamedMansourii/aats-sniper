"""aats-telegram scaffold service package (paper-only placeholder).

The real Telegram alert + de-risk command channel is implemented in
`aats.telegram` (T-360/T-361). This package is the compose entrypoint shim
that boots the service process, serves /health on METRICS_PORT, and runs as
a long-lived scaffold until the full wiring lands.

HARD RULES (paper-only, enforced):
  - DRY_RUN_ENABLED=true is the compile-time default; the process exits with
    code 1 if the env var is explicitly set to false (real capital gate).
  - No Telegram bot token is read or used; no HTTP calls are made.
  - No secrets, keys, or real values in this package.
"""
