"""T-361 — command-bot config loading + SECRET DISCIPLINE tests.

Proves the non-waivable secret rules for the de-risk command channel:
  * The bot token / operator API token are read from the environment as VAULT
    REFERENCES (custody-policy.md §4/§7) — never key material, never a literal.
  * Placeholder refs => `enabled` is False (offline posture; offline client).
  * `safe_repr` never echoes a token ref in the clear and never lists the
    operator user IDs (only a count).
  * The shipped T-361 module source contains NO secret literal (token / API key).
  * DRY-RUN defaults True (real capital disabled by default — HARD RULE), and the
    command channel exposes NO way to enable LIVE.
"""

from __future__ import annotations

import pathlib
import re

from aats.telegram.command_config import TelegramCommandConfig

# Token-shaped literal detector (Telegram bot token / long opaque API token).
_REAL_TOKEN_PATTERN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# A bearer-ish long secret literal.
_LONG_SECRET_PATTERN = re.compile(r"['\"][A-Za-z0-9_\-]{40,}['\"]")

_TELEGRAM_PKG_DIR = pathlib.Path(__file__).resolve().parents[2] / "aats" / "telegram"
_T361_MODULES = ("command_config.py", "commands.py", "control_plane_client.py")


def test_from_env_disabled_on_placeholder_refs():
    env = {
        "TELEGRAM_OPERATOR_USER_IDS": "<your-telegram-user-id>",
        "TELEGRAM_BOT_TOKEN_VAULT_REF": "secret/aats/telegram-bot-token",
        "OPERATOR_API_TOKEN": "<vault-ref: secret/aats/operator-api-token>",
        "DRY_RUN_ENABLED": "true",
    }
    cfg = TelegramCommandConfig.from_env(env)
    # API token is a placeholder -> offline posture.
    assert cfg.enabled is False
    assert cfg.dry_run_enabled is True
    # Placeholder operator-id => empty allowlist => no operator (fail-closed).
    assert cfg.operator_user_ids == frozenset()


def test_from_env_enabled_with_concrete_refs():
    env = {
        "TELEGRAM_OPERATOR_USER_IDS": "424242",
        "TELEGRAM_BOT_TOKEN_VAULT_REF": "secret/aats/telegram-bot-token",
        "OPERATOR_API_TOKEN": "concrete-operator-api-token-value",
        "CONTROL_PLANE_URL": "http://control:8787",
        "DRY_RUN_ENABLED": "false",
    }
    cfg = TelegramCommandConfig.from_env(env)
    assert cfg.enabled is True
    assert cfg.operator_user_ids == frozenset({424242})
    assert cfg.control_plane_url == "http://control:8787"
    # Even 'enabled' and DRY-RUN off, safe_repr never echoes the token.
    assert "concrete-operator-api-token-value" not in cfg.safe_repr()


def test_from_env_missing_keys_defaults_failclosed_and_dryrun():
    cfg = TelegramCommandConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.operator_user_ids == frozenset()  # no operator => fail-closed
    assert cfg.dry_run_enabled is True  # real capital disabled by default


def test_safe_repr_hides_tokens_and_operator_ids():
    cfg = TelegramCommandConfig(
        operator_user_ids=frozenset({424242, 555111}),
        bot_token_vault_ref="987654321:AAfakeSecretMustStayHidden9999999999",
        operator_api_token="another-long-secret-token-value-hidden",
        control_plane_url="http://localhost:8787",
    )
    rep = cfg.safe_repr()
    assert "MustStayHidden" not in rep
    assert "another-long-secret" not in rep
    # The operator user IDs themselves are not listed — only a count.
    assert "424242" not in rep
    assert "555111" not in rep
    assert "operator_count=2" in rep


def test_no_secret_literal_in_t361_source():
    """The shipped T-361 module source contains no real token/secret literal."""
    offenders: list[str] = []
    for name in _T361_MODULES:
        py = _TELEGRAM_PKG_DIR / name
        text = py.read_text(encoding="utf-8")
        for m in _REAL_TOKEN_PATTERN.finditer(text):
            offenders.append(f"{name}: token-shaped {m.group(0)!r}")
        for m in _LONG_SECRET_PATTERN.finditer(text):
            offenders.append(f"{name}: long-secret {m.group(0)!r}")
    assert offenders == [], f"secret-shaped literal in T-361 source: {offenders}"
