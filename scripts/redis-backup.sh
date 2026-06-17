#!/usr/bin/env bash
# ============================================================
# AATS Redis state snapshot backup
# ============================================================
# PURPOSE
#   Take a point-in-time snapshot of the Redis data volume
#   (the named Docker volume `aats_redis-data`), compress it,
#   and store it in BACKUP_DIR with a datestamp.
#
# WHAT IS BACKED UP
#   The Redis dump.rdb file inside the `aats_redis-data` Docker
#   volume. Redis is configured (docker-compose.yml) with:
#     --save 60 1
#   meaning it flushes a snapshot to disk every 60 seconds if at
#   least 1 key changed. The backup script triggers a BGSAVE first
#   to ensure the snapshot is current before copying.
#
# WHAT THIS BACKS UP (and what it does NOT)
#   BACKED UP:
#     - Redis KV state (circuit-breaker state, FSM state, feature
#       hot-tier values, DMS heartbeat, tip-fee cache)
#     - Redis Streams (pending snipe events, slow-loop results)
#   NOT BACKED UP HERE (separate backup recommended):
#     - Prometheus metrics volume (prometheus-data) — use
#       Prometheus's own snapshot API or rsync the volume directly.
#     - Grafana volume (grafana-data) — contains dashboards and
#       provisioned config; back up with a separate rsync.
#     - The recorded R1/R2 launch corpus — that is the operator's
#       primary trading asset, stored outside Docker volumes.
#
# RESTORE:
#   See docs/redis-backup-restore.md for the full procedure.
#   Short form: stop redis, copy dump.rdb into the volume, restart.
#
# USAGE (standalone):
#   bash scripts/redis-backup.sh
#
# ENVIRONMENT (all have defaults):
#   BACKUP_DIR       — destination directory (default: /var/backups/aats)
#   RETENTION_DAYS   — how many days of backups to keep (default: 14)
#   REDIS_CONTAINER  — docker container name for redis (default: aats-redis-1)
#   BACKUP_LOG       — log file path; empty = stdout only
#   DRY_RUN_BACKUP   — if "true", print actions but do not write (smoke-test mode)
#
# INVOKED BY:
#   deploy/systemd/aats-backup.service (via systemd timer)
#   or manually: bash scripts/redis-backup.sh
#
# HARD RULES:
#   - DRY_RUN_ENABLED is NOT consulted here. This script never submits
#     transactions or interacts with trading logic. It only reads Redis.
#   - No secrets are written to backup files or logs.
#   - The backup is READ-ONLY from Redis's perspective (BGSAVE, then
#     docker cp). The running Redis instance is never stopped.
#   - A failed backup emits a log line and exits non-zero, which
#     triggers the systemd backup unit to surface a failed-unit alert.
#     It does NOT stop, restart, or interfere with the trading stack.
#
# infrastructure.md §1 / deploy/colocation-rpc-plan.md §Pre-Live Checklist
# ============================================================

set -euo pipefail

# ── Configuration (with defaults) ───────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/var/backups/aats}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
REDIS_CONTAINER="${REDIS_CONTAINER:-aats-redis-1}"
BACKUP_LOG="${BACKUP_LOG:-}"
DRY_RUN_BACKUP="${DRY_RUN_BACKUP:-false}"

TIMESTAMP="$(date -u '+%Y%m%d_%H%M%S')"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Logging helpers ──────────────────────────────────────────────────────────
_log() {
    local level="$1"; shift
    local msg="[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [${level}] $*"
    echo "${msg}"
    if [[ -n "${BACKUP_LOG}" ]]; then
        echo "${msg}" >> "${BACKUP_LOG}" 2>/dev/null || true
    fi
}

info()  { _log "INFO" "$@"; }
warn()  { _log "WARN" "$@"; }
error() { _log "ERROR" "$@"; }

# ── Guards ───────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    error "docker not found. Cannot run backup. Install Docker on this host."
    exit 1
fi

# ── Dry-run mode (smoke test — verify the script logic without writing) ──────
if [[ "${DRY_RUN_BACKUP}" == "true" ]]; then
    info "DRY_RUN_BACKUP=true — printing actions without writing."
    info "Would create backup dir: ${BACKUP_DIR}"
    info "Would trigger BGSAVE on container: ${REDIS_CONTAINER}"
    info "Would copy dump.rdb from container to: ${BACKUP_DIR}/redis-dump_${TIMESTAMP}.rdb"
    info "Would compress to: ${BACKUP_DIR}/redis-dump_${TIMESTAMP}.rdb.gz"
    info "Would prune backups older than ${RETENTION_DAYS} days from ${BACKUP_DIR}"
    info "DRY_RUN_BACKUP complete — no files written."
    exit 0
fi

# ── Check the Redis container is running ─────────────────────────────────────
if ! docker ps --filter "name=^/${REDIS_CONTAINER}$" --filter "status=running" \
        --format "{{.Names}}" | grep -q "^${REDIS_CONTAINER}$"; then
    error "Redis container '${REDIS_CONTAINER}' is not running. Cannot back up."
    error "Check: docker ps | grep redis"
    error "If the container name differs, set: REDIS_CONTAINER=<name>"
    exit 1
fi

# ── Create the backup directory ───────────────────────────────────────────────
info "Backup dir: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}" || {
    error "Cannot create backup directory '${BACKUP_DIR}'. Check permissions."
    exit 1
}

# ── Trigger a BGSAVE to ensure the snapshot is current ───────────────────────
info "Triggering BGSAVE on Redis container '${REDIS_CONTAINER}'..."
BGSAVE_OUTPUT=$(docker exec "${REDIS_CONTAINER}" redis-cli BGSAVE 2>&1) || {
    error "BGSAVE command failed: ${BGSAVE_OUTPUT}"
    exit 1
}
info "BGSAVE response: ${BGSAVE_OUTPUT}"

# Wait for BGSAVE to complete (poll LASTSAVE up to 30 seconds)
LASTSAVE_BEFORE=$(docker exec "${REDIS_CONTAINER}" redis-cli LASTSAVE 2>&1)
info "LASTSAVE before: ${LASTSAVE_BEFORE}"
BGSAVE_TIMEOUT=30
BGSAVE_ELAPSED=0
while true; do
    LASTSAVE_AFTER=$(docker exec "${REDIS_CONTAINER}" redis-cli LASTSAVE 2>&1)
    if [[ "${LASTSAVE_AFTER}" != "${LASTSAVE_BEFORE}" ]]; then
        info "BGSAVE complete. New LASTSAVE: ${LASTSAVE_AFTER}"
        break
    fi
    if [[ "${BGSAVE_ELAPSED}" -ge "${BGSAVE_TIMEOUT}" ]]; then
        warn "BGSAVE did not complete within ${BGSAVE_TIMEOUT}s (LASTSAVE unchanged). Copying last known snapshot."
        break
    fi
    sleep 1
    BGSAVE_ELAPSED=$((BGSAVE_ELAPSED + 1))
done

# ── Copy the dump.rdb from the container ─────────────────────────────────────
BACKUP_RDB="${BACKUP_DIR}/redis-dump_${TIMESTAMP}.rdb"
info "Copying dump.rdb from container '${REDIS_CONTAINER}:/data/dump.rdb' to '${BACKUP_RDB}'..."
docker cp "${REDIS_CONTAINER}:/data/dump.rdb" "${BACKUP_RDB}" || {
    error "Failed to copy dump.rdb from container. Is the path /data/dump.rdb correct?"
    error "Check: docker exec ${REDIS_CONTAINER} ls /data/"
    exit 1
}
info "Copied: ${BACKUP_RDB} ($(du -sh "${BACKUP_RDB}" | cut -f1))"

# ── Compress the backup ───────────────────────────────────────────────────────
info "Compressing backup..."
gzip -9 "${BACKUP_RDB}" || {
    error "gzip compression failed for '${BACKUP_RDB}'."
    exit 1
}
BACKUP_GZ="${BACKUP_RDB}.gz"
info "Compressed: ${BACKUP_GZ} ($(du -sh "${BACKUP_GZ}" | cut -f1))"

# ── Verify the compressed file is valid ──────────────────────────────────────
if gzip -t "${BACKUP_GZ}" 2>/dev/null; then
    info "Integrity check passed: ${BACKUP_GZ} is a valid gzip archive."
else
    error "Integrity check FAILED for '${BACKUP_GZ}'. The backup may be corrupt."
    exit 1
fi

# ── Prune old backups (retention) ────────────────────────────────────────────
info "Pruning backups older than ${RETENTION_DAYS} days from '${BACKUP_DIR}'..."
PRUNED=$(find "${BACKUP_DIR}" -maxdepth 1 -name 'redis-dump_*.rdb.gz' \
    -mtime "+${RETENTION_DAYS}" -print -delete 2>&1) || true
if [[ -n "${PRUNED}" ]]; then
    info "Pruned: ${PRUNED}"
else
    info "No old backups to prune."
fi

# ── List current backups ──────────────────────────────────────────────────────
BACKUP_COUNT=$(find "${BACKUP_DIR}" -maxdepth 1 -name 'redis-dump_*.rdb.gz' | wc -l)
info "Backup complete. ${BACKUP_COUNT} backup(s) now in '${BACKUP_DIR}'."
info "Most recent: ${BACKUP_GZ}"

# ── Done ─────────────────────────────────────────────────────────────────────
exit 0
