# AATS Redis Backup and Restore Procedure

**Owner:** `latency-devops-engineer`
**Status:** Operational (E5 — always-on operational hardening)
**Companion files:**
- `scripts/redis-backup.sh` — automated backup script
- `deploy/systemd/aats-backup.service` — one-shot backup unit
- `deploy/systemd/aats-backup.timer` — daily schedule (03:00 UTC)
- `deploy/logrotate/aats` — log rotation config

---

## 1. What is backed up (and what is not)

### Backed up by `scripts/redis-backup.sh`

| Asset | Where | Why |
|---|---|---|
| `redis-data` Docker volume (`dump.rdb`) | `BACKUP_DIR/redis-dump_YYYYMMDD_HHMMSS.rdb.gz` | Circuit-breaker state, FSM state, feature hot-tier, DMS heartbeat epoch, tip-fee cache |

### Not backed up by this script (back up separately)

| Asset | Recommended method |
|---|---|
| Prometheus metrics volume (`prometheus-data`) | `prometheus snapshot` API, or rsync the volume at known-quiescent time |
| Grafana volume (`grafana-data`) | rsync `/var/lib/docker/volumes/aats_grafana-data/_data/` |
| R1/R2 recorded launch corpus | operator-managed; stored outside Docker volumes |

### What is NOT in Redis (so restoring Redis does NOT lose it)

- Decision logs (structlog JSON lines) — written to the decision log volume, rotated by logrotate
- Net PnL history — in Prometheus TSDB (`prometheus-data`)
- Trade records (if persisted to a database) — outside Redis

---

## 2. Backup schedule and retention

| Item | Value |
|---|---|
| Schedule | Daily at 03:00 UTC (via `aats-backup.timer`) |
| Retention | 14 days (configurable via `RETENTION_DAYS` in `.env` or the service unit) |
| Location | `BACKUP_DIR` (default: `/var/backups/aats`) |
| Filename pattern | `redis-dump_YYYYMMDD_HHMMSS.rdb.gz` |

---

## 3. Running a backup manually

```bash
# Trigger an immediate backup (one-shot, same as the timer does):
sudo systemctl start aats-backup

# Or run the script directly:
sudo bash /opt/aats/scripts/redis-backup.sh

# Dry-run (smoke test — prints actions without writing):
DRY_RUN_BACKUP=true bash /opt/aats/scripts/redis-backup.sh

# Override backup directory:
BACKUP_DIR=/mnt/nas/aats-backups bash /opt/aats/scripts/redis-backup.sh
```

---

## 4. Restore procedure

### 4.1 Preconditions (read before touching anything)

1. **Confirm `DRY_RUN_ENABLED=true`** before restoring. A restore should never be performed while the trading stack is attempting to submit live transactions.
2. **Identify the backup file** to restore from:
   ```bash
   ls -lh /var/backups/aats/redis-dump_*.rdb.gz | tail -10
   ```
3. **Note the circuit-breaker state** after restore. The backup captures the breaker at its state at backup time. If the breaker was `TRIPPED` at backup time, it restores `TRIPPED` — this is correct and intentional (fail-safe default).

### 4.2 Stop the trading stack

```bash
# Stop all services except those you want to keep (or stop everything):
cd /opt/aats
docker compose stop aats-hotcore aats-slow aats-controlplane aats-dms aats-telegram aats-signer
# Leave redis running during the copy IF you take an active snapshot (see step 4.3).
# OR stop redis first if you want a guaranteed-quiescent restore:
docker compose stop redis
```

### 4.3 Copy the backup into the volume

```bash
# Identify the backup to restore:
BACKUP_FILE=/var/backups/aats/redis-dump_20260617_030001.rdb.gz

# Decompress the backup:
TMPDIR=$(mktemp -d)
gunzip -c "${BACKUP_FILE}" > "${TMPDIR}/dump.rdb"

# Verify the decompressed file:
ls -lh "${TMPDIR}/dump.rdb"

# Find the volume mount point on the host:
docker volume inspect aats_redis-data --format '{{ .Mountpoint }}'
# Output example: /var/lib/docker/volumes/aats_redis-data/_data

# Copy dump.rdb into the volume data directory (as root):
VOLUME_PATH=$(docker volume inspect aats_redis-data --format '{{ .Mountpoint }}')
sudo cp "${TMPDIR}/dump.rdb" "${VOLUME_PATH}/dump.rdb"

# Verify:
ls -lh "${VOLUME_PATH}/dump.rdb"

# Clean up temp dir:
rm -rf "${TMPDIR}"
```

### 4.4 Restart Redis

```bash
cd /opt/aats
docker compose start redis

# Wait for Redis to come healthy:
docker compose ps redis
# Status should be: "healthy" after ~5s

# Verify Redis loaded the dump:
docker exec aats-redis-1 redis-cli DBSIZE
# Should be > 0 if the backup contained data.
```

### 4.5 Verify circuit-breaker and DMS state post-restore

```bash
# Check circuit-breaker state:
docker exec aats-redis-1 redis-cli GET aats:circuit_breaker:state
# Expected values: ARMED | TRIPPED | COOLDOWN
# If TRIPPED: the breaker was tripped at backup time. Manual review required.
# Reset only if the conditions that tripped it are resolved.

# Check DMS heartbeat (if the key is missing, the DMS will use a fresh epoch):
docker exec aats-redis-1 redis-cli GET aats:dms:last_heartbeat
```

### 4.6 Restart the full stack

```bash
cd /opt/aats
docker compose up -d

# Verify health:
docker compose ps
curl -s http://localhost:8787/api/health | python3 -m json.tool
```

### 4.7 Post-restore checklist

- [ ] Dashboard shows correct mode (SHADOW / PAPER — not LIVE unless intentional)
- [ ] Circuit-breaker state is as expected (ARMED or intentionally TRIPPED)
- [ ] DMS heartbeat is being updated (Grafana: `aats_dms_heartbeat_age_seconds` < 60)
- [ ] Geyser feed staleness is normal (< 800 ms)
- [ ] No P1 alerts firing unexpectedly

---

## 5. What Redis state does NOT need to be restored

The following are rebuilt automatically on startup:

| State | Rebuild mechanism |
|---|---|
| Tip-fee cache | Polled fresh from Jito REST/WS on hotcore startup |
| Feature hot-tier | Rebuilt by the slow loop on the first few ticks |
| Pre-signed DMS flatten transactions | Re-issued by `aats-dms` as positions are reconciled |

The only state that is genuinely lost on Redis wipe:
- Circuit-breaker trip records (restored to ARMED on a clean state — conservatively safe)
- Any in-flight snipe decisions queued in Redis Streams at the moment of failure

In the event of an unrecoverable Redis failure without a backup, restarting with a fresh Redis is safe: the bot resumes in SHADOW/PAPER mode, the breaker is ARMED (the fail-safe default), and the DMS re-issues flattens for any residual open positions.

---

## 6. Off-site backup (recommended for R3+)

For live-capital operations, copy backups to an off-host location:

```bash
# Example: sync to an S3-compatible bucket (s3cmd, aws cli, or rclone):
rclone sync /var/backups/aats/ s3:my-aats-backups/redis/ --max-age 30d

# Or to a remote host via rsync:
rsync -az /var/backups/aats/ backup-host:/mnt/aats-backups/redis/
```

Off-site backup is the operator's responsibility. The `redis-backup.sh` script handles only local disk. Extend via `ExecStartPost` in `aats-backup.service` or a separate systemd unit.

---

## 7. Install / verify the backup system

```bash
# Install systemd units (from repo root on the Linux deploy host):
sudo cp deploy/systemd/aats-backup.service /etc/systemd/system/
sudo cp deploy/systemd/aats-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start the timer (fires daily at 03:00 UTC):
sudo systemctl enable --now aats-backup.timer

# Verify the timer is scheduled:
systemctl list-timers --all | grep aats

# Run a test backup immediately:
sudo systemctl start aats-backup
sudo journalctl -u aats-backup -n 30

# Verify the backup file was created:
ls -lh /var/backups/aats/redis-dump_*.rdb.gz

# Dry-run the restore decompression step:
BACKUP_FILE=$(ls -t /var/backups/aats/redis-dump_*.rdb.gz | head -1)
TMPDIR=$(mktemp -d)
gunzip -c "${BACKUP_FILE}" > "${TMPDIR}/dump.rdb"
ls -lh "${TMPDIR}/dump.rdb"
rm -rf "${TMPDIR}"
echo "Restore dry-run: OK"
```
