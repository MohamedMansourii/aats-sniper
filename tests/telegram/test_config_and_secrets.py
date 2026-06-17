"""T-360 — config loading + SECRET DISCIPLINE tests.

Proves the non-waivable secret rules:
  * The bot token is read from the environment (.env.example schema) — never a
    literal in code.
  * `redact_token` masks the secret portion of any token-shaped string so a real
    token can never reach a log line, an exception, or a captured message.
  * The package SOURCE contains NO secret (only placeholders / env reads) — a
    grep of the shipped module source finds no token-shaped literal.
  * The offline client captures message text + chat_id but NEVER a token.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from aats.telegram.client import HttpTelegramClient, OfflineTelegramClient
from aats.telegram.config import TelegramAlertConfig, redact_token

# A Telegram bot token is "<digits>:<35 base64-ish chars>".  This pattern detects
# a *real* token literal (used to assert none ship in source).
_REAL_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")

_TELEGRAM_PKG_DIR = pathlib.Path(__file__).resolve().parents[2] / "aats" / "telegram"


# ---------------------------------------------------------------------------
# redact_token
# ---------------------------------------------------------------------------


def test_redact_token_masks_secret_portion():
    redacted = redact_token("123456789:AAFakeSecretPortionThatMustNeverLeak1234")
    assert "AAFakeSecret" not in redacted
    assert "MustNeverLeak" not in redacted
    assert redacted == "123456:<redacted>"


def test_redact_token_handles_unset_and_placeholder():
    assert redact_token(None) == "<unset>"
    assert redact_token("") == "<unset>"
    assert redact_token("<alertmanager-bot-token-or-vault-ref>") == "<placeholder>"


def test_redact_token_masks_colonless_secret():
    assert redact_token("rawsecretnoColon") == "<redacted>"


def test_config_safe_repr_never_shows_token():
    cfg = TelegramAlertConfig(
        bot_token="987654321:AAAnotherFakeSecretThatMustStayHidden99",
        chat_id="-1001234567890",
        dry_run_enabled=True,
        enabled=True,
    )
    rep = cfg.safe_repr()
    assert "AAAnotherFake" not in rep
    assert "MustStayHidden" not in rep
    assert "987654:<redacted>" in rep


# ---------------------------------------------------------------------------
# Config from env (injectable env dict — no real os.environ dependency)
# ---------------------------------------------------------------------------


def test_from_env_disabled_on_placeholder():
    env = {
        "ALERTMANAGER_TELEGRAM_BOT_TOKEN": "<alertmanager-bot-token-or-vault-ref>",
        "ALERTMANAGER_TELEGRAM_CHAT_ID": "<your-chat-id>",
        "DRY_RUN_ENABLED": "true",
    }
    cfg = TelegramAlertConfig.from_env(env)
    assert cfg.enabled is False  # placeholders -> offline posture
    assert cfg.dry_run_enabled is True


def test_from_env_enabled_with_concrete_values():
    env = {
        "ALERTMANAGER_TELEGRAM_BOT_TOKEN": "111222333:AAconcreteRuntimeInjectedTokenValueX",
        "ALERTMANAGER_TELEGRAM_CHAT_ID": "-1009876543210",
        "DRY_RUN_ENABLED": "false",
    }
    cfg = TelegramAlertConfig.from_env(env)
    assert cfg.enabled is True
    assert cfg.dry_run_enabled is False
    # Even when 'enabled', a safe_repr never echoes the token.
    assert "concreteRuntime" not in cfg.safe_repr()


def test_from_env_missing_keys_defaults_to_disabled():
    cfg = TelegramAlertConfig.from_env({})
    assert cfg.enabled is False
    # DRY-RUN defaults to True (real capital disabled by default — HARD RULE).
    assert cfg.dry_run_enabled is True


# ---------------------------------------------------------------------------
# NO SECRET IN CODE — grep the shipped package source
# ---------------------------------------------------------------------------


def test_no_real_token_literal_in_package_source():
    """The shipped aats/telegram/*.py source contains no real token literal."""
    offenders: list[str] = []
    for py in _TELEGRAM_PKG_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in _REAL_TOKEN_PATTERN.finditer(text):
            offenders.append(f"{py.name}: {m.group(0)}")
    assert offenders == [], f"token-shaped literal in package source: {offenders}"


def test_no_obvious_secret_keywords_with_values_in_source():
    """No assignment of a bot token to a literal in the package source."""
    suspicious = re.compile(r"(bot_token|BOT_TOKEN)\s*=\s*['\"]\d{6,}:[A-Za-z0-9_-]{20,}")
    for py in _TELEGRAM_PKG_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not suspicious.search(text), f"hardcoded bot token in {py.name}"


# ---------------------------------------------------------------------------
# Offline client never stores a token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_client_captures_text_not_token():
    client = OfflineTelegramClient()
    await client.send_message("-100123", "✅ FILL\ntoken: DOGE2")
    assert len(client.sent) == 1
    captured = client.sent[0]
    assert captured.chat_id == "-100123"
    assert "FILL" in captured.text
    # SentMessage has no token field at all.
    assert not hasattr(captured, "token")
    assert not hasattr(captured, "bot_token")


@pytest.mark.asyncio
async def test_offline_client_simulates_transient_failure():
    client = OfflineTelegramClient()
    client.fail_next = True
    with pytest.raises(RuntimeError):
        await client.send_message("-100123", "x")
    # After the simulated failure, the next send succeeds.
    await client.send_message("-100123", "y")
    assert client.texts == ["y"]


def test_http_client_requires_token_but_holds_it_privately():
    """HttpTelegramClient won't construct without a token, and never exposes it."""
    with pytest.raises(ValueError):
        HttpTelegramClient("")
    # We do not construct a real one (no httpx network here); constructing with a
    # placeholder still must not surface the token on the public surface.
    # (Construction touches httpx.AsyncClient, so we only assert the guard above.)
