# AATS systemd units (E5 — always-on operational hardening)

These units are an **alternative to running `docker compose up` by hand**.
They do not replace `docker-compose.yml` — they wrap it.

**Kubernetes is explicitly out of scope** (ADR-0011 — single-node bot; no control-plane RTT tax until a documented multi-node trigger fires).

---

## Units

| Unit | Purpose |
|---|---|
| `aats.service` | Full AATS stack — all 11 services, startup self-check pre-start gate |
| `aats-controlplane.service` | Control plane + observability only (no hotcore/DMS); useful during maintenance |
| `aats-backup.service` | One-shot Redis snapshot backup (invoked by the timer or manually) |
| `aats-backup.timer` | Daily schedule — fires `aats-backup.service` at 03:00 UTC |

---

## Quick install (Linux deploy host, run as root)

```bash
# From the repo root on the deploy host (/opt/aats):
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo cp deploy/systemd/*.timer   /etc/systemd/system/

sudo systemctl daemon-reload

# Enable the trading stack to start on boot:
sudo systemctl enable aats

# Enable the daily backup timer:
sudo systemctl enable --now aats-backup.timer

# Start the trading stack now:
sudo systemctl start aats
sudo systemctl status aats
```

---

## Syntax check

```bash
# On a host with systemd >= 237:
systemd-analyze verify /etc/systemd/system/aats.service
systemd-analyze verify /etc/systemd/system/aats-backup.service
systemd-analyze verify /etc/systemd/system/aats-backup.timer
systemd-analyze verify /etc/systemd/system/aats-controlplane.service
```

---

## Safety contract

- `DRY_RUN_ENABLED` is **never set to `false` in any unit here**. The compose default of `true` applies. The operator must set it explicitly in `.env` after completing `docs/pre-live-checklist.md`.
- The `aats.service` unit runs `scripts/startup-self-check.sh` as `ExecStartPre`. If the self-check fails (e.g. DRY_RUN disabled without PRE_LIVE_CHECKLIST_SIGNED), the compose stack does NOT start.
- A failed backup (`aats-backup.service`) surfaces as a systemd unit failure and journal entry. It does NOT stop, restart, or interfere with the trading stack.

---

## Backup and restore

See `docs/redis-backup-restore.md` for the full backup/restore procedure and off-site backup guidance.
