"""T-361 — Injectable control-plane client for the Telegram COMMAND bot.

The control-plane HTTP API is an EXTERNAL service and is NOT reachable in this
environment.  Per the build directive it is INJECTABLE behind a small protocol
so the command handlers can be exercised offline with a deterministic fake.

STRUCTURAL DE-RISK-ONLY GUARANTEE (the load-bearing design choice):
  This client exposes EXACTLY FOUR methods, and every one of them is a de-risk
  or a read:

    * get_status()       — GET  /api/state + /api/metrics  (READ)
    * kill()             — POST /api/kill                  (DE-RISK: halt + flatten)
    * flatten_mint(mint) — POST /api/flatten/{mint}        (DE-RISK: exit one)
    * pause()            — POST /api/mode {SHADOW}          (DE-RISK: step mode DOWN)

  There is NO method to size up, widen a stop, enable LIVE, advance mode toward
  live, reset the breaker, or tighten/widen risk-config.  `pause()` posts a
  HARD-CODED downward mode (`SHADOW`) — the destination mode is NOT a parameter,
  so a caller (hostile or buggy) CANNOT use this client to move mode UP the
  ladder.  "Operator surfaces may only de-risk" is therefore enforced by the
  SHAPE of this seam, not merely by a runtime check (BUILD-DIRECTIVE HARD RULES;
  api-contracts.md §7; custody-policy.md §7).

SECRET DISCIPLINE: the operator Bearer token lives only inside the HTTP adapter
instance and is placed into the `Authorization` header at call time; it is
redacted from every log line via `redact_token` and never captured by the
offline fake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from aats.telegram.config import redact_token

log = logging.getLogger(__name__)

# The single hard-coded de-risk pause target.  `/pause` steps the agent mode
# DOWN to SHADOW (record-only, submit-nothing) — the deepest de-risk posture on
# the staging ladder (api-contracts.md §2).  This is a constant, NEVER a caller
# argument, so the pause command structurally cannot move mode UP toward LIVE.
PAUSE_TARGET_MODE = "SHADOW"


@dataclass(frozen=True)
class StatusSnapshot:
    """A read-only, de-risk-irrelevant status view for the /status command.

    Built from GET /api/state + GET /api/metrics (api-contracts.md §4).  Money
    fields are carried VERBATIM as the contract emits them — integer lamports or
    decimal-string — and are NEVER parsed to float here (the formatter uses
    Decimal).  There is NO win-rate / outcome-rate field (AC-037).
    """

    mode: str
    killed: bool
    breaker_tripped: bool
    dry_run_enabled: bool
    open_positions: int
    # Decimal-as-string straight off the wire (NFR-009) — never float.
    net_pnl_sol: str
    daily_pnl_sol: str
    daily_loss_limit_sol: str
    # Integer lamports off the wire (NFR-009) — never float.
    wallet_balance_lamports: int
    geyser_connected: bool


@dataclass(frozen=True)
class CommandOutcome:
    """The result of routing a de-risk command to the control plane.

    `ok` is True on a contract success status (202 for kill/flatten, 200 for the
    downward mode change).  `detail` is a short, secret-free human string for the
    operator.  No internal stack trace or token ever rides here.
    """

    ok: bool
    detail: str


@runtime_checkable
class ControlPlaneClient(Protocol):
    """The de-risk-only seam the command handlers depend on.

    DELIBERATELY MINIMAL: only read + de-risk operations.  No method on this
    protocol can increase risk.  Implementations MUST NOT log the operator token.
    """

    async def get_status(self) -> StatusSnapshot: ...
    async def kill(self) -> CommandOutcome: ...
    async def flatten_mint(self, mint: str) -> CommandOutcome: ...
    async def pause(self) -> CommandOutcome: ...


@dataclass
class OfflineControlPlaneClient:
    """Deterministic offline fake — the default in tests and DRY-RUN/dev.

    Records every de-risk call in `calls` (an ordered list of (op, arg) tuples)
    so tests can assert that a command routed to the CORRECT control-plane
    operation and ONLY de-risked.  NO network, NO token, NO secret.

    `status` backs get_status(); mutate it to simulate state.  `fail_next` makes
    the next de-risk call return a handled failure outcome so error handling is
    exercised offline (the seam never raises a transport error into the handler;
    it returns CommandOutcome(ok=False)).
    """

    status: StatusSnapshot = field(
        default_factory=lambda: StatusSnapshot(
            mode="SHADOW",
            killed=False,
            breaker_tripped=False,
            dry_run_enabled=True,
            open_positions=0,
            net_pnl_sol="0.0",
            daily_pnl_sol="0.0",
            daily_loss_limit_sol="-0.30",
            wallet_balance_lamports=0,
            geyser_connected=True,
        )
    )
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    fail_next: bool = False

    def _maybe_fail(self, op: str) -> CommandOutcome | None:
        if self.fail_next:
            self.fail_next = False
            # The call is still recorded so tests can assert routing happened.
            return CommandOutcome(ok=False, detail=f"{op} failed (offline-simulated)")
        return None

    async def get_status(self) -> StatusSnapshot:
        self.calls.append(("get_status", None))
        return self.status

    async def kill(self) -> CommandOutcome:
        self.calls.append(("kill", None))
        failed = self._maybe_fail("kill")
        if failed is not None:
            return failed
        return CommandOutcome(ok=True, detail="kill accepted (entries halted, positions flattening)")

    async def flatten_mint(self, mint: str) -> CommandOutcome:
        self.calls.append(("flatten_mint", mint))
        failed = self._maybe_fail("flatten")
        if failed is not None:
            return failed
        return CommandOutcome(ok=True, detail=f"flatten accepted for {mint}")

    async def pause(self) -> CommandOutcome:
        # The destination mode is the module constant, NOT an argument — the
        # offline fake records the literal de-risk target it would post.
        self.calls.append(("pause", PAUSE_TARGET_MODE))
        failed = self._maybe_fail("pause")
        if failed is not None:
            return failed
        return CommandOutcome(ok=True, detail=f"paused (mode -> {PAUSE_TARGET_MODE})")

    @property
    def ops(self) -> list[str]:
        """Convenience: the ordered operation names recorded (tests)."""
        return [op for op, _ in self.calls]

    def reset(self) -> None:
        self.calls.clear()
        self.fail_next = False


def _status_from_wire(state: dict[str, Any], metrics: dict[str, Any]) -> StatusSnapshot:
    """Build a StatusSnapshot from raw /api/state + /api/metrics wire dicts.

    Money fields are carried VERBATIM (decimal-string / integer lamports); a
    JSON-float in a monetary field is rejected (money is never float — NFR-009).
    """
    wallet = state.get("wallet") or {}
    bal = wallet.get("balance_lamports", 0)
    if isinstance(bal, bool) or not isinstance(bal, int):
        raise TypeError(
            f"wallet.balance_lamports must be int lamports, got {type(bal).__name__} "
            f"(money is never float — NFR-009)."
        )
    connection = state.get("connection") or {}
    return StatusSnapshot(
        mode=str(state.get("mode", "?")),
        killed=bool(state.get("killed", False)),
        breaker_tripped=bool(state.get("breaker_tripped", False)),
        dry_run_enabled=bool(state.get("dry_run_enabled", True)),
        open_positions=int(metrics.get("open_positions", 0)),
        net_pnl_sol=str(metrics.get("net_pnl_sol", "0")),
        daily_pnl_sol=str(metrics.get("daily_pnl_sol", "0")),
        daily_loss_limit_sol=str(metrics.get("daily_loss_limit_sol", "0")),
        wallet_balance_lamports=bal,
        geyser_connected=bool(connection.get("geyser", False)),
    )


class HttpControlPlaneClient:
    """Real control-plane adapter (httpx).  Constructed at runtime only.

    NOT exercised in tests (no network here).  The operator Bearer token lives
    only in this instance, is placed into the `Authorization` header at call
    time, and is redacted from every log line.  A bounded timeout is enforced on
    every call so a hung control plane cannot stall the command channel.

    DE-RISK-ONLY BY CONSTRUCTION: only the four de-risk/read methods exist; the
    only mode this adapter ever POSTs is the hard-coded `PAUSE_TARGET_MODE`
    (SHADOW) — it has no code path that can post LIVE / advance the ladder.
    """

    def __init__(
        self,
        base_url: str,
        operator_api_token: str,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        if not operator_api_token:
            raise ValueError(
                "HttpControlPlaneClient requires an operator API token (runtime-injected)."
            )
        self._base = base_url.rstrip("/")
        self._token = operator_api_token
        self._timeout_s = timeout_s
        # Import httpx lazily so the package imports offline without the dep
        # resolved (tests never reach this branch).
        import httpx

        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=timeout_s)

    @property
    def _auth_headers(self) -> dict[str, str]:
        # The Bearer token is assembled only at call time; never logged.
        return {"Authorization": f"Bearer {self._token}"}

    async def get_status(self) -> StatusSnapshot:
        try:
            state_resp = await self._client.get(
                f"{self._base}/api/state", headers=self._auth_headers
            )
            state_resp.raise_for_status()
            metrics_resp = await self._client.get(
                f"{self._base}/api/metrics", headers=self._auth_headers
            )
            metrics_resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — re-raised after redaction
            raise RuntimeError(
                f"control-plane status read failed (op {redact_token(self._token)}): "
                f"{type(exc).__name__}"
            ) from None
        return _status_from_wire(state_resp.json(), metrics_resp.json())

    async def _post_derisk(self, path: str, op: str, *, body: dict | None = None) -> CommandOutcome:
        """POST a de-risk endpoint; map status to a CommandOutcome (never raises).

        A transport/HTTP error is converted to CommandOutcome(ok=False) with a
        secret-free detail — the command channel must never surface a stack trace
        or a token.  202/200 => ok.
        """
        try:
            resp = await self._client.post(
                f"{self._base}{path}", headers=self._auth_headers, json=body
            )
        except Exception as exc:  # noqa: BLE001 — handled, secret-free
            log.warning("control-plane %s transport error: %s", op, type(exc).__name__)
            return CommandOutcome(ok=False, detail=f"{op} failed (control plane unreachable)")
        if resp.status_code in (200, 202):
            return CommandOutcome(ok=True, detail=f"{op} accepted")
        log.warning("control-plane %s returned HTTP %d", op, resp.status_code)
        return CommandOutcome(ok=False, detail=f"{op} rejected (HTTP {resp.status_code})")

    async def kill(self) -> CommandOutcome:
        return await self._post_derisk("/api/kill", "kill")

    async def flatten_mint(self, mint: str) -> CommandOutcome:
        # urllib-encode the mint into the path; control plane treats {mint} as
        # URL-encoded (api-contracts.md §5).  httpx encodes the path segment.
        from urllib.parse import quote

        safe = quote(mint, safe="")
        return await self._post_derisk(f"/api/flatten/{safe}", "flatten")

    async def pause(self) -> CommandOutcome:
        # The ONLY mode this adapter can ever post is the hard-coded downward
        # target (SHADOW).  There is, by construction, no parameter and no branch
        # that can post a higher mode — the pause command cannot increase risk.
        return await self._post_derisk("/api/mode", "pause", body={"mode": PAUSE_TARGET_MODE})

    async def aclose(self) -> None:
        await self._client.aclose()
