"""T-361 — Telegram CONSTRAINED de-risk-only command set tests.

Proves the BUILD-DIRECTIVE / custody-policy.md §7 acceptance for the command
channel, driven entirely against an OFFLINE control-plane client (no network):

  AUTHZ
    * ONLY the configured operator user-ID is authorized.
    * An unauthorized sender is REJECTED + logged, with NO control-plane call.
    * A missing / non-int / bot sender is rejected (fail-closed).
    * An EMPTY allowlist authorizes no one (fail-closed default).

  ROUTING (de-risk only)
    * /status routes to get_status (read).
    * /kill   (after confirm) routes to POST /api/kill.
    * /flatten <mint> (after confirm) routes to POST /api/flatten/{mint} for
      EXACTLY that mint.
    * /pause routes to POST /api/mode {SHADOW} — a DOWNWARD (de-risk) mode only.

  STRUCTURAL DE-RISK-ONLY
    * No command verb can size up / widen / enable LIVE / advance mode toward
      live — such a command DOES NOT EXIST on the surface or the client seam.
    * The pause target is the hard-coded SHADOW constant (not a parameter).

  PER-COMMAND CONFIRM
    * /kill and /flatten do NOT fire on first invocation; they require a confirm
      nonce bound to the SAME operator, single-use, TTL-bounded.

  MONEY / HONESTY
    * /status formats money via Decimal (integer lamports / decimal-string),
      never float; there is NO win-rate / outcome-rate field anywhere.
"""

from __future__ import annotations

import inspect

import pytest

from aats.telegram.command_config import TelegramCommandConfig, _parse_operator_ids
from aats.telegram.commands import (
    _KNOWN_COMMANDS,
    TelegramCommandHandler,
    _format_status,
    _parse_command,
)
from aats.telegram.control_plane_client import (
    PAUSE_TARGET_MODE,
    ControlPlaneClient,
    HttpControlPlaneClient,
    OfflineControlPlaneClient,
    StatusSnapshot,
)
from tests.telegram.conftest import (
    NON_OPERATOR_USER_ID,
    OPERATOR_USER_ID,
    make_update,
)


def _handler(command_config, offline_control_plane, **kw) -> TelegramCommandHandler:
    return TelegramCommandHandler(
        config=command_config, client=offline_control_plane, **kw
    )


# ===========================================================================
# AUTHZ — operator-ID-only (the central VERIFY requirement)
# ===========================================================================


@pytest.mark.asyncio
async def test_only_operator_is_authorized(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update("/status", user_id=OPERATOR_USER_ID))
    assert reply is not None
    assert reply.authorized is True
    assert reply.fired is True
    # The operator's /status routed to the read endpoint.
    assert offline_control_plane.ops == ["get_status"]


@pytest.mark.asyncio
async def test_unauthorized_sender_rejected_no_api_call(
    command_config, offline_control_plane, caplog
):
    h = _handler(command_config, offline_control_plane)
    import logging

    with caplog.at_level(logging.WARNING):
        reply = await h.handle_update(
            make_update("/kill", user_id=NON_OPERATOR_USER_ID)
        )
    # Rejected: not authorized, nothing fired, and NO control-plane call at all.
    assert reply is not None
    assert reply.authorized is False
    assert reply.fired is False
    assert offline_control_plane.calls == []  # ZERO control-plane calls
    # The rejection was logged (without echoing the command body).
    assert any("REJECTED unauthorized sender" in r.message for r in caplog.records)
    # The full user-ID is NOT splashed into the log line (only a fingerprint).
    assert all(str(NON_OPERATOR_USER_ID) not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unauthorized_status_makes_no_call(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update("/status", user_id=NON_OPERATOR_USER_ID))
    assert reply is not None and reply.authorized is False and reply.fired is False
    assert offline_control_plane.calls == []


@pytest.mark.asyncio
async def test_missing_sender_rejected_failclosed(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update("/status", user_id=None))
    assert reply is not None and reply.authorized is False
    assert offline_control_plane.calls == []


@pytest.mark.asyncio
async def test_empty_allowlist_authorizes_no_one(offline_control_plane):
    # An empty operator allowlist is fail-closed: NO sender is an operator.
    cfg = TelegramCommandConfig(
        operator_user_ids=frozenset(),
        bot_token_vault_ref="<ref>",
        operator_api_token="<ref>",
        control_plane_url="http://localhost:8787",
    )
    h = _handler(cfg, offline_control_plane)
    reply = await h.handle_update(make_update("/status", user_id=OPERATOR_USER_ID))
    assert reply is not None and reply.authorized is False
    assert offline_control_plane.calls == []


def test_is_operator_rejects_bool_and_non_int(command_config):
    # bool is an int subclass but must NEVER pass as a user-ID.
    assert command_config.is_operator(True) is False
    assert command_config.is_operator(None) is False
    assert command_config.is_operator(NON_OPERATOR_USER_ID) is False
    assert command_config.is_operator(OPERATOR_USER_ID) is True


def test_parse_operator_ids_drops_placeholders_and_garbage():
    assert _parse_operator_ids("<your-telegram-user-id>") == frozenset()
    assert _parse_operator_ids("") == frozenset()
    assert _parse_operator_ids(None) == frozenset()
    assert _parse_operator_ids("123, 456 789") == frozenset({123, 456, 789})
    # A malformed entry is dropped, valid ones kept (never crash, never widen).
    assert _parse_operator_ids("123, notanid, 456") == frozenset({123, 456})


# ===========================================================================
# ROUTING — kill / flatten / pause hit the correct de-risk endpoint
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_routes_to_kill_after_confirm(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    # First /kill issues a confirm prompt and does NOT fire.
    prompt = await h.handle_update(make_update("/kill"))
    assert prompt is not None and prompt.fired is False
    assert prompt.confirm_nonce is not None
    assert offline_control_plane.calls == []  # nothing fired yet
    assert "CONFIRM REQUIRED" in prompt.text

    # The matching confirm fires POST /api/kill.
    done = await h.handle_update(make_update(f"/confirm {prompt.confirm_nonce}"))
    assert done is not None and done.fired is True
    assert offline_control_plane.ops == ["kill"]


@pytest.mark.asyncio
async def test_flatten_routes_to_correct_mint_after_confirm(
    command_config, offline_control_plane
):
    h = _handler(command_config, offline_control_plane)
    mint = "So11111111111111111111111111111111111111112"
    prompt = await h.handle_update(make_update(f"/flatten {mint}"))
    assert prompt is not None and prompt.fired is False
    assert offline_control_plane.calls == []

    done = await h.handle_update(make_update(f"/confirm {prompt.confirm_nonce}"))
    assert done is not None and done.fired is True
    # Routed to flatten for EXACTLY that mint (per-mint de-risk, AC-044).
    assert offline_control_plane.calls == [("flatten_mint", mint)]


@pytest.mark.asyncio
async def test_flatten_requires_a_mint(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update("/flatten"))
    assert reply is not None and reply.fired is False
    assert "usage" in reply.text.lower()
    assert offline_control_plane.calls == []


@pytest.mark.asyncio
async def test_pause_routes_to_downward_mode_only(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    # /pause needs no confirm (already de-risk) and fires immediately.
    reply = await h.handle_update(make_update("/pause"))
    assert reply is not None and reply.fired is True
    # Routed to pause with the hard-coded DOWNWARD target (SHADOW) — never UP.
    assert offline_control_plane.calls == [("pause", PAUSE_TARGET_MODE)]
    assert PAUSE_TARGET_MODE == "SHADOW"


@pytest.mark.asyncio
async def test_status_routes_to_read_only(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update("/status"))
    assert reply is not None and reply.fired is True
    assert offline_control_plane.ops == ["get_status"]
    assert "STATUS" in reply.text


# ===========================================================================
# STRUCTURAL DE-RISK-ONLY — no risk-increasing command exists
# ===========================================================================


def test_command_set_is_exactly_the_four_derisk_commands():
    # The closed command set contains ONLY de-risk / read verbs.
    expected = frozenset({"status", "kill", "flatten", "pause"})
    assert expected == _KNOWN_COMMANDS


@pytest.mark.parametrize(
    "hostile",
    [
        "/live",
        "/golive",
        "/mode LIVE",
        "/sizeup 5",
        "/size 1000000000",
        "/widen",
        "/leverage 10",
        "/breaker_reset",
        "/reset",
        "/risk widen",
        "/enable",
    ],
)
@pytest.mark.asyncio
async def test_no_risk_increasing_command_exists(
    command_config, offline_control_plane, hostile
):
    # Even from the AUTHORIZED operator, a risk-increasing verb does not exist:
    # it never reaches any control-plane call.
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update(hostile, user_id=OPERATOR_USER_ID))
    # Either ignored or an "unknown command" reply — but NEVER a fired call.
    assert reply is None or reply.fired is False
    assert offline_control_plane.calls == []


def test_control_plane_client_seam_exposes_only_derisk_methods():
    # The seam itself is the structural guard: no risk-increasing method exists.
    public = {
        n
        for n in dir(OfflineControlPlaneClient)
        if not n.startswith("_") and callable(getattr(OfflineControlPlaneClient, n))
    }
    # Exclude pure test/telemetry helpers; the ACTION surface is exactly these:
    action_methods = {"get_status", "kill", "flatten_mint", "pause"}
    assert action_methods.issubset(public)
    # No method name hints at sizing up / widening / going live.
    forbidden_substrings = ("size", "widen", "live", "leverage", "increase", "buy", "enter")
    for name in public:
        assert not any(s in name.lower() for s in forbidden_substrings), name


def test_pause_target_is_a_constant_not_a_parameter():
    # pause() on the seam takes NO mode argument — the destination is fixed.
    sig = inspect.signature(OfflineControlPlaneClient.pause)
    # Only `self`.
    params = list(sig.parameters)
    assert params == ["self"]


# ===========================================================================
# PER-COMMAND CONFIRM — binding, single-use, TTL, operator-match
# ===========================================================================


@pytest.mark.asyncio
async def test_confirm_is_single_use(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    prompt = await h.handle_update(make_update("/kill"))
    nonce = prompt.confirm_nonce
    first = await h.handle_update(make_update(f"/confirm {nonce}"))
    assert first.fired is True
    # A replay of the same nonce does NOT re-fire (single-use).
    replay = await h.handle_update(make_update(f"/confirm {nonce}"))
    assert replay.fired is False
    assert offline_control_plane.ops == ["kill"]  # exactly one kill


@pytest.mark.asyncio
async def test_confirm_bound_to_same_operator(offline_control_plane):
    # Two operators in the allowlist; one cannot consume the other's confirm.
    other_op = 555111
    cfg = TelegramCommandConfig(
        operator_user_ids=frozenset({OPERATOR_USER_ID, other_op}),
        bot_token_vault_ref="<ref>",
        operator_api_token="<ref>",
        control_plane_url="http://localhost:8787",
    )
    h = _handler(cfg, offline_control_plane)
    prompt = await h.handle_update(make_update("/kill", user_id=OPERATOR_USER_ID))
    # A DIFFERENT (also-allowlisted) operator tries to use the nonce.
    hijack = await h.handle_update(
        make_update(f"/confirm {prompt.confirm_nonce}", user_id=other_op)
    )
    assert hijack.fired is False
    assert offline_control_plane.calls == []  # nothing fired


@pytest.mark.asyncio
async def test_confirm_expires_after_ttl(command_config, offline_control_plane):
    # Inject a controllable clock to drive the TTL deterministically.
    now = {"t": 1000.0}
    h = _handler(
        command_config,
        offline_control_plane,
        clock=lambda: now["t"],
        confirm_ttl_s=30.0,
    )
    prompt = await h.handle_update(make_update("/kill"))
    now["t"] += 31.0  # advance past TTL
    expired = await h.handle_update(make_update(f"/confirm {prompt.confirm_nonce}"))
    assert expired.fired is False
    assert "expired" in expired.text.lower()
    assert offline_control_plane.calls == []


@pytest.mark.asyncio
async def test_invalid_confirm_nonce_does_nothing(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    reply = await h.handle_update(make_update("/confirm deadbeefdeadbeef"))
    assert reply.fired is False
    assert offline_control_plane.calls == []


@pytest.mark.asyncio
async def test_pending_confirm_is_bounded(command_config, offline_control_plane):
    h = _handler(command_config, offline_control_plane)
    # Issue many confirm prompts; the pending table stays bounded.
    for i in range(200):
        await h.handle_update(make_update(f"/flatten Mint{i}"))
    assert h.pending_confirm_count <= 64


# ===========================================================================
# MONEY (Decimal, never float) + HONESTY (no win-rate)
# ===========================================================================


def _status_snapshot(**kw) -> StatusSnapshot:
    base = {
        "mode": "LIVE_DRY_RUN",
        "killed": False,
        "breaker_tripped": False,
        "dry_run_enabled": True,
        "open_positions": 2,
        "net_pnl_sol": "-0.0123",
        "daily_pnl_sol": "-0.0123",
        "daily_loss_limit_sol": "-0.30",
        "wallet_balance_lamports": 1_500_000_000,
        "geyser_connected": True,
    }
    base.update(kw)
    return StatusSnapshot(**base)  # type: ignore[arg-type]


def test_status_formats_money_via_decimal_no_winrate():
    snap = _status_snapshot()
    text = _format_status(snap)
    # Integer lamports rendered via Decimal (1.5 SOL).
    assert "+1.500000 SOL" in text
    # Decimal-string PnL surfaced verbatim.
    assert "-0.0123 SOL" in text
    assert "-0.30 SOL" in text
    # HONESTY CLAUSE — no win-rate / outcome-rate field anywhere (AC-037).
    lowered = text.lower()
    for banned in ("win rate", "win-rate", "winrate", "hit rate", "hit-rate", "hitrate", "success rate"):
        assert banned not in lowered


def test_status_money_field_rejects_float_lamports():
    # The formatter (shared) rejects a float lamport quantity (NFR-009).
    snap = _status_snapshot(wallet_balance_lamports=1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _format_status(snap)


def test_status_shows_dry_run_posture_and_halt_state():
    snap = _status_snapshot(killed=True, breaker_tripped=True, dry_run_enabled=True)
    text = _format_status(snap)
    assert "DRY-RUN" in text
    assert "KILLED" in text
    assert "BREAKER-TRIPPED" in text


# ===========================================================================
# ERROR DISCIPLINE — a control-plane failure never crashes / leaks
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_failure_is_handled_gracefully(command_config, offline_control_plane):
    offline_control_plane.fail_next = True
    h = _handler(command_config, offline_control_plane)
    prompt = await h.handle_update(make_update("/kill"))
    done = await h.handle_update(make_update(f"/confirm {prompt.confirm_nonce}"))
    # The call was still routed (kill recorded) but the outcome is a clean,
    # secret-free failure message — no crash, no stack trace.
    assert offline_control_plane.ops == ["kill"]
    assert done.fired is True
    assert "⚠️" in done.text


@pytest.mark.asyncio
async def test_status_unreachable_is_handled(command_config):
    class _Boom:
        async def get_status(self):
            raise ConnectionError("control plane down")

        async def kill(self):  # pragma: no cover - not used here
            ...

        async def flatten_mint(self, mint):  # pragma: no cover
            ...

        async def pause(self):  # pragma: no cover
            ...

    h = TelegramCommandHandler(config=command_config, client=_Boom())
    reply = await h.handle_update(make_update("/status"))
    assert reply is not None
    assert "unavailable" in reply.text.lower()
    # No internal detail / stack trace leaks to the operator.
    assert "ConnectionError" not in reply.text


# ===========================================================================
# Parsing + seam protocol conformance
# ===========================================================================


def test_parse_command_handles_botname_and_args():
    assert _parse_command("/status") == ("status", None)
    assert _parse_command("/flatten ABC") == ("flatten", "ABC")
    assert _parse_command("/kill@aats_bot") == ("kill", None)
    assert _parse_command("/flatten@aats_bot MintX") == ("flatten", "MintX")
    assert _parse_command("not a command") == (None, None)
    assert _parse_command("") == (None, None)


def test_offline_client_satisfies_protocol(offline_control_plane):
    assert isinstance(offline_control_plane, ControlPlaneClient)


def test_http_client_requires_api_token():
    with pytest.raises(ValueError):
        HttpControlPlaneClient("http://localhost:8787", "")
