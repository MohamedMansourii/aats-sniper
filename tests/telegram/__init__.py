"""T-360 — Telegram ALERT channel test suite (fully offline).

Every external service (Telegram HTTP, the control-plane feed, the breaker
signal) is replaced by a deterministic in-memory fake.  No network, no secrets,
no real bot token anywhere in this suite.
"""
