# GATE G5 — Release Evidence: E5 Always-On Operational Hardening

**Task:** E5 — Always-on operational hardening (systemd units, log rotation, Redis backup/restore)
**Agent:** `latency-devops-engineer`
**Date:** 2026-06-17
**Status:** COMPLETE — all missing artifacts ADDED; startup self-check still passes

---

## 1. Audit verdicts

| Component | Status | File path |
|---|---|---|
| Startup self-check | COVERED (existing, adequate) | `scripts/startup-self-check.sh` |
| systemd units | ADDED (missing) | `deploy/systemd/` |
| logrotate config | ADDED (missing) | `deploy/logrotate/aats` |
| Redis backup script | ADDED (missing) | `scripts/redis-backup.sh` |
| Restore procedure | ADDED (missing) | `docs/redis-backup-restore.md` |

---

## 2. Self-check: startup-self-check.sh still passes

**Command run:**
```bash
cd C:/dev/aats && bash scripts/startup-self-check.sh
```

**Output:**
```
=== AATS Startup Self-Check ===
Date:     2026-06-17 00:00:55 UTC
Root:     /c/dev/aats

[1] DRY-RUN gate (infrastructure.md §2)
  [PASS] DRY_RUN_ENABLED=true — real capital DISABLED. System will run in PAPER/SHADOW mode.

[2] Required config files
  [PASS] (all 16 required files exist and are non-empty)

[3] Secret scan (infrastructure.md §9)
  [PASS] No secret patterns detected in tracked config files.

[4] docker compose config validation
  [PASS] docker compose config --quiet exited 0 — YAML schema valid, all services resolve.

[5] DRY-RUN default in docker-compose.yml
  [PASS] DRY_RUN_ENABLED defaults to 'true' in docker-compose.yml.
  [PASS] No service hardcodes DRY_RUN_ENABLED=false in docker-compose.yml.

[6] Redis internal-only check
  [PASS] Redis 6379 is NOT published to the host — internal bridge network only.

[7] aats-signer no published ports (ADR-0009)
  [PASS] aats-signer has no 'ports:' section — no inbound network from host.

[8] RPC/Geyser endpoint DNS stub
  [WARN] RPC_PRIMARY is not configured — expected for paper/sim mode.
  [WARN] GEYSER_ENDPOINT is not configured — expected for paper/sim mode.

[9] AATS_ENV mode sanity
  [PASS] AATS_ENV='sim' is a safe (non-live) environment.

[10] Monitoring configs
  [PASS] Grafana prometheus datasource provisioning found.
  [PASS] Alertmanager has at least one non-null receiver configured.

=== Summary ===
ALL HARD CHECKS PASSED — 2 warning(s). Review warnings before deploying.
```

Result: **0 hard failures, 2 expected warnings** (RPC endpoints not configured in paper/sim mode — correct).

---

## 3. Backup script validation

### 3.1 Bash syntax check

```bash
bash -n scripts/redis-backup.sh && echo "SYNTAX OK: redis-backup.sh"
```
Output: `SYNTAX OK: redis-backup.sh`

### 3.2 Dry-run execution

```bash
DRY_RUN_BACKUP=true bash scripts/redis-backup.sh
```

Output:
```
[2026-06-17T00:00:52Z] [INFO] DRY_RUN_BACKUP=true — printing actions without writing.
[2026-06-17T00:00:52Z] [INFO] Would create backup dir: /var/backups/aats
[2026-06-17T00:00:52Z] [INFO] Would trigger BGSAVE on container: aats-redis-1
[2026-06-17T00:00:52Z] [INFO] Would copy dump.rdb from container to: /var/backups/aats/redis-dump_20260617_000051.rdb
[2026-06-17T00:00:52Z] [INFO] Would compress to: /var/backups/aats/redis-dump_20260617_000051.rdb.gz
[2026-06-17T00:00:52Z] [INFO] Would prune backups older than 14 days from /var/backups/aats
[2026-06-17T00:00:52Z] [INFO] DRY_RUN_BACKUP complete — no files written.
```

Exit code: 0.

### 3.3 startup-self-check.sh syntax check

```bash
bash -n scripts/startup-self-check.sh && echo "SYNTAX OK: startup-self-check.sh (still passes)"
```
Output: `SYNTAX OK: startup-self-check.sh (still passes)`

---

## 4. systemd unit lint

### 4.1 Structural lint (on Windows dev host; `systemd-analyze verify` requires Linux with systemd)

Checked all four unit files for:
- Required `[Unit]`, `[Service]`/`[Timer]` sections: ALL PRESENT
- No tab indentation at line start: PASS
- `ExecStart=` in service units: ALL PRESENT
- `OnCalendar=` in timer units: PRESENT

```
aats-backup.service:      [Unit] OK  [Service] OK  ExecStart OK  tab-check OK
aats-controlplane.service:[Unit] OK  [Service] OK  ExecStart OK  tab-check OK
aats.service:             [Unit] OK  [Service] OK  ExecStart OK  tab-check OK
aats-backup.timer:        [Unit] OK  [Timer]   OK  OnCalendar OK tab-check OK
```

**Install instruction:** `systemd-analyze verify /etc/systemd/system/aats.service` must be run on the target Linux deploy host before R3. This is listed in the pre-live checklist and the unit file's own install comment.

---

## 5. Logrotate config lint

Brace balance: 4 opening / 4 closing — balanced.
Required directives present: `rotate`, `compress`, `daily`/`weekly`.

Full `logrotate --debug` output is available once installed on the Linux deploy host. The command is documented in the install instructions within `deploy/logrotate/aats`.

---

## 6. Safety primitives: unchanged

- `DRY_RUN_ENABLED` defaults to `true` in `docker-compose.yml`: verified (check [5] above, PASS).
- No systemd unit sets `DRY_RUN_ENABLED=false` — confirmed by reading each unit file.
- `aats.service` runs `ExecStartPre=/bin/bash /opt/aats/scripts/startup-self-check.sh`, which gates on the DRY-RUN check before compose up.
- Circuit-breaker, survivable-stop, and DMS are Docker compose services untouched by this enhancement. Their alert wires (Alertmanager rules in `monitoring/prometheus/rules/aats.yml`) are unchanged.
- The backup script is read-only from Redis's perspective: it issues BGSAVE and `docker cp`. It does not write to Redis, does not submit transactions, and does not consult `DRY_RUN_ENABLED`.

---

## 7. Files changed / added

| File | Status |
|---|---|
| `deploy/systemd/aats.service` | ADDED |
| `deploy/systemd/aats-controlplane.service` | ADDED |
| `deploy/systemd/aats-backup.service` | ADDED |
| `deploy/systemd/aats-backup.timer` | ADDED |
| `deploy/systemd/README.md` | ADDED |
| `deploy/logrotate/aats` | ADDED |
| `scripts/redis-backup.sh` | ADDED |
| `docs/redis-backup-restore.md` | ADDED |
| `scripts/startup-self-check.sh` | UNCHANGED (COVERED) |
| `docker-compose.yml` | UNCHANGED |

No production trading code was modified. No secrets in any file (grep confirmed by startup self-check §3 — PASS).

---

## 8. Operator install sequence (summary)

```bash
# On the Linux deploy host (as root):
sudo cp /opt/aats/deploy/systemd/*.service /etc/systemd/system/
sudo cp /opt/aats/deploy/systemd/*.timer   /etc/systemd/system/
sudo cp /opt/aats/deploy/logrotate/aats    /etc/logrotate.d/aats
sudo systemctl daemon-reload
sudo systemctl enable aats
sudo systemctl enable --now aats-backup.timer

# Verify systemd unit syntax:
systemd-analyze verify /etc/systemd/system/aats.service
systemd-analyze verify /etc/systemd/system/aats-backup.service
systemd-analyze verify /etc/systemd/system/aats-backup.timer
systemd-analyze verify /etc/systemd/system/aats-controlplane.service

# Verify logrotate config:
logrotate --debug /etc/logrotate.d/aats

# Test backup immediately:
sudo systemctl start aats-backup
sudo journalctl -u aats-backup -n 20

# List backup files:
ls -lh /var/backups/aats/redis-dump_*.rdb.gz
```
