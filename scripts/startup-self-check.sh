#!/usr/bin/env bash
# ============================================================
# AATS startup self-check — runs before `docker compose up`
# ============================================================
# Purpose: verify the local configuration is safe and coherent
# before any service starts. This is NOT a substitute for the
# safety rails inside the services — it is a last-ditch sanity
# gate at the shell level.
#
# Invoked by the operator before docker compose up:
#   bash scripts/startup-self-check.sh && docker compose up -d
#
# All checks are READ-ONLY. This script does NOT start services,
# does NOT modify configuration, and does NOT submit any transaction.
#
# Exit 0 = all checks pass.
# Exit 1 = at least one HARD FAIL — do NOT bring up the stack.
#
# HARD RULES this script enforces:
#   1. DRY_RUN_ENABLED must be "true" (default) unless the operator
#      explicitly and intentionally sets it. If it is "false", the
#      script warns loudly and requires a confirm unless
#      PRE_LIVE_CHECKLIST_SIGNED=yes is also set.
#   2. No secret material may appear in .env (if present) or in
#      docker-compose.yml. The script greps for raw key patterns.
#   3. The docker compose config must parse cleanly (schema lint).
#   4. Required config files must exist and be non-empty.
#   5. RPC/Geyser endpoints (if configured) must resolve at the DNS
#      level as a basic connectivity stub.
#
# infrastructure.md §2 (DRY-RUN hard constraint), §9 (secrets policy),
# BUILD-DIRECTIVE HARD RULES.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

HARD_FAIL=0
WARN_COUNT=0

print_header() {
    echo -e "\n${CYAN}${BOLD}=== AATS Startup Self-Check ===${RESET}"
    echo -e "${CYAN}Date:     $(date -u '+%Y-%m-%d %H:%M:%S UTC')${RESET}"
    echo -e "${CYAN}Root:     ${ROOT_DIR}${RESET}"
    echo ""
}

pass() { echo -e "  ${GREEN}[PASS]${RESET} $*"; }
warn() { echo -e "  ${YELLOW}[WARN]${RESET} $*"; WARN_COUNT=$((WARN_COUNT + 1)); }
fail() { echo -e "  ${RED}[FAIL]${RESET} $*"; HARD_FAIL=1; }
info() { echo -e "  ${CYAN}[INFO]${RESET} $*"; }

# ── 1. DRY-RUN gate (the most important check) ─────────────────────────────
check_dry_run() {
    echo -e "${BOLD}[1] DRY-RUN gate (infrastructure.md §2)${RESET}"

    local dry_run="${DRY_RUN_ENABLED:-true}"

    if [[ "${dry_run}" == "true" ]]; then
        pass "DRY_RUN_ENABLED=true — real capital DISABLED. System will run in PAPER/SHADOW mode."
    elif [[ "${dry_run}" == "false" ]]; then
        echo ""
        echo -e "  ${RED}${BOLD}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!${RESET}"
        echo -e "  ${RED}${BOLD}  WARNING: DRY_RUN_ENABLED=false detected in environment.       ${RESET}"
        echo -e "  ${RED}${BOLD}  REAL CAPITAL CAN BE DEPLOYED if a CEO auth token is provided. ${RESET}"
        echo -e "  ${RED}${BOLD}  This is only legal at staging rung R3 with explicit CEO auth. ${RESET}"
        echo -e "  ${RED}${BOLD}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!${RESET}"
        echo ""

        # Require the pre-live checklist sign-off
        if [[ "${PRE_LIVE_CHECKLIST_SIGNED:-no}" != "yes" ]]; then
            fail "PRE_LIVE_CHECKLIST_SIGNED is not set to 'yes'. The pre-live checklist (deploy/colocation-rpc-plan.md §Pre-Live Checklist) must be completed and PRE_LIVE_CHECKLIST_SIGNED=yes must be set before bringing up the stack with DRY_RUN_ENABLED=false."
        else
            warn "DRY_RUN_ENABLED=false with PRE_LIVE_CHECKLIST_SIGNED=yes. Proceeding to live-mode checks."

            # Additional live-mode requirements
            if [[ -z "${CEO_AUTH_TOKEN:-}" ]]; then
                fail "CEO_AUTH_TOKEN is required for LIVE mode (AC-060) but is not set."
            else
                pass "CEO_AUTH_TOKEN is set (not validated here; the control-plane enforces it)."
            fi

            if [[ -z "${WALLET_PUBKEY:-}" ]]; then
                fail "WALLET_PUBKEY must be set for LIVE mode."
            else
                pass "WALLET_PUBKEY is set: ${WALLET_PUBKEY:0:8}..."
            fi

            if [[ -z "${VAULT_ADDR:-}" ]] || [[ -z "${VAULT_TOKEN:-}" ]]; then
                fail "VAULT_ADDR and VAULT_TOKEN must both be set for aats-signer secret fetch (infrastructure.md §5.3)."
            else
                pass "VAULT_ADDR and VAULT_TOKEN are set."
            fi
        fi
    else
        fail "DRY_RUN_ENABLED has an unexpected value '${dry_run}'. Must be 'true' or 'false'."
    fi
}

# ── 2. Required config files ────────────────────────────────────────────────
check_config_files() {
    echo -e "\n${BOLD}[2] Required config files${RESET}"

    local required_files=(
        "docker-compose.yml"
        "docker/Dockerfile.hotcore"
        "docker/Dockerfile.signer"
        "docker/Dockerfile.bot"
        "docker/Dockerfile.controlplane"
        "docker/Dockerfile.dms"
        "docker/Dockerfile.telegram"
        "docker/Dockerfile.dashboard"
        "docker/nginx.conf"
        "monitoring/prometheus/prometheus.yml"
        "monitoring/prometheus/rules/aats.yml"
        "monitoring/alertmanager/alertmanager.yml"
        "monitoring/grafana/provisioning/datasources/prometheus.yml"
        "monitoring/grafana/provisioning/dashboards/dashboards.yml"
        "config/program-allowlist.json"
        ".env.example"
    )

    for f in "${required_files[@]}"; do
        local path="${ROOT_DIR}/${f}"
        if [[ -f "${path}" ]] && [[ -s "${path}" ]]; then
            pass "Exists and non-empty: ${f}"
        elif [[ -f "${path}" ]]; then
            warn "Exists but EMPTY: ${f}"
        else
            fail "MISSING: ${f}"
        fi
    done
}

# ── 3. Secret scan (no raw key material in tracked files) ───────────────────
check_no_secrets() {
    echo -e "\n${BOLD}[3] Secret scan (infrastructure.md §9)${RESET}"

    local targets=(
        "docker-compose.yml"
        "monitoring/prometheus/prometheus.yml"
        "monitoring/alertmanager/alertmanager.yml"
    )

    local secret_patterns=(
        "PRIVATE_KEY\s*=\s*[A-Za-z0-9]{40,}"
        "SECRET\s*=\s*[A-Za-z0-9]{32,}"
        "sk-[A-Za-z0-9]{40,}"
        "-----BEGIN.*PRIVATE KEY-----"
        "vault\.token\s*=\s*hvs\."
    )

    local found_secret=0
    for f in "${targets[@]}"; do
        local path="${ROOT_DIR}/${f}"
        [[ -f "${path}" ]] || continue
        for pattern in "${secret_patterns[@]}"; do
            if grep -qEi "${pattern}" "${path}" 2>/dev/null; then
                fail "Secret pattern '${pattern}' found in ${f}"
                found_secret=1
            fi
        done
    done

    # Check .env if it exists (it should NOT be committed; check it at runtime)
    if [[ -f "${ROOT_DIR}/.env" ]]; then
        warn ".env file found on disk. Ensure it is in .gitignore and contains NO raw key material."
        # Check for raw Solana private key (base58, 87-88 chars)
        if grep -qE "^(WALLET_PRIVATE_KEY|WALLET_SECRET|KEYPAIR_JSON)\s*=" "${ROOT_DIR}/.env" 2>/dev/null; then
            fail "Raw wallet key variable found in .env — this violates infrastructure.md §5.3. Use Vault references only."
        fi
        # Check for real-looking OpenAI keys
        if grep -qE "sk-[A-Za-z0-9]{40,}" "${ROOT_DIR}/.env" 2>/dev/null; then
            fail "Possible real OpenAI API key in .env. Move to Vault reference."
        fi
    fi

    if [[ "${found_secret}" -eq 0 ]]; then
        pass "No secret patterns detected in tracked config files."
    fi
}

# ── 4. docker compose config validation ─────────────────────────────────────
check_compose_config() {
    echo -e "\n${BOLD}[4] docker compose config validation${RESET}"

    if ! command -v docker &>/dev/null; then
        warn "docker not found — skipping compose config validation. Run 'docker compose config' manually on the deploy host."
        return
    fi

    # Build minimal env for compose config parsing
    local compose_env=(
        "DRY_RUN_ENABLED=${DRY_RUN_ENABLED:-true}"
        "AATS_ENV=${AATS_ENV:-sim}"
        "VAULT_ADDR=${VAULT_ADDR:-https://vault.example.com}"
        "VAULT_TOKEN=${VAULT_TOKEN:-placeholder}"
        "WALLET_PUBKEY=${WALLET_PUBKEY:-placeholder}"
        "GEYSER_ENDPOINT=${GEYSER_ENDPOINT:-https://placeholder.example.com}"
        "GEYSER_TOKEN=${GEYSER_TOKEN:-placeholder}"
        "RPC_PRIMARY=${RPC_PRIMARY:-https://placeholder.example.com}"
        "RPC_SECONDARY=${RPC_SECONDARY:-https://placeholder.example.com}"
        "JITO_BLOCK_ENGINE=${JITO_BLOCK_ENGINE:-https://frankfurt.mainnet.block-engine.jito.wtf}"
        "T_DMS_SECONDS=${T_DMS_SECONDS:-60}"
        "CEO_AUTH_TOKEN=${CEO_AUTH_TOKEN:-placeholder}"
        "TELEGRAM_BOT_TOKEN_VAULT_REF=${TELEGRAM_BOT_TOKEN_VAULT_REF:-placeholder}"
        "TELEGRAM_OPERATOR_USER_IDS=${TELEGRAM_OPERATOR_USER_IDS:-0}"
        "ALERTMANAGER_TELEGRAM_BOT_TOKEN=${ALERTMANAGER_TELEGRAM_BOT_TOKEN:-placeholder}"
        "ALERTMANAGER_TELEGRAM_CHAT_ID=${ALERTMANAGER_TELEGRAM_CHAT_ID:-0}"
        "ALERTMANAGER_PAGERDUTY_KEY=${ALERTMANAGER_PAGERDUTY_KEY:-}"
        "OPENAI_API_KEY=${OPENAI_API_KEY:-}"
        "SOCIAL_API_KEY=${SOCIAL_API_KEY:-}"
        "VITE_USE_MOCK=${VITE_USE_MOCK:-true}"
        "VITE_CONTROL_PLANE_URL=${VITE_CONTROL_PLANE_URL:-http://localhost:8787}"
        "GRAFANA_ADMIN_USER=${GRAFANA_ADMIN_USER:-admin}"
        "GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:-placeholder}"
    )

    local output
    if output=$(cd "${ROOT_DIR}" && env "${compose_env[@]}" docker compose config --quiet 2>&1); then
        pass "docker compose config --quiet exited 0 — YAML schema valid, all services resolve."
    else
        fail "docker compose config failed:\n${output}"
    fi
}

# ── 5. Safety primitives: DRY-RUN gate in compose ───────────────────────────
check_dry_run_in_compose() {
    echo -e "\n${BOLD}[5] DRY-RUN default in docker-compose.yml${RESET}"

    local compose_file="${ROOT_DIR}/docker-compose.yml"

    # Verify the default expression appears
    if grep -q 'DRY_RUN_ENABLED.*:-true' "${compose_file}"; then
        pass "DRY_RUN_ENABLED defaults to 'true' in docker-compose.yml."
    else
        fail "DRY_RUN_ENABLED default of 'true' not found in docker-compose.yml — the compose file may allow live submission without an explicit env override."
    fi

    # Verify no service overrides DRY_RUN_ENABLED to false
    if grep -E 'DRY_RUN_ENABLED\s*:\s*"false"' "${compose_file}" | grep -v '#'; then
        fail "A service in docker-compose.yml sets DRY_RUN_ENABLED: \"false\" — this is forbidden in the compose file."
    else
        pass "No service hardcodes DRY_RUN_ENABLED=false in docker-compose.yml."
    fi
}

# ── 6. Redis internal-only (no host port) ────────────────────────────────────
check_redis_not_published() {
    echo -e "\n${BOLD}[6] Redis internal-only check${RESET}"

    local compose_file="${ROOT_DIR}/docker-compose.yml"

    # After the redis service definition, check that no ports: section publishes 6379
    # Simple heuristic: grep for 6379 in a ports: context
    if grep -E '^\s+-\s+"?6379:6379"?' "${compose_file}" | grep -v '#'; then
        fail "Redis port 6379 appears to be published to the host. Redis must be internal-only (infrastructure.md §1)."
    else
        pass "Redis 6379 is NOT published to the host — internal bridge network only."
    fi
}

# ── 7. aats-signer no published ports ────────────────────────────────────────
check_signer_no_published_ports() {
    echo -e "\n${BOLD}[7] aats-signer no published ports (ADR-0009)${RESET}"

    # Parse the compose file to check the signer service has no ports: key with host bindings
    # Simple check: between "aats-signer:" and the next top-level service, look for "ports:"
    local in_signer=0
    local found_published=0
    while IFS= read -r line; do
        if echo "${line}" | grep -qE '^  aats-signer:'; then
            in_signer=1
        elif echo "${line}" | grep -qE '^  [a-z]' && [[ "${in_signer}" -eq 1 ]]; then
            in_signer=0
        fi
        if [[ "${in_signer}" -eq 1 ]] && echo "${line}" | grep -qE '^\s+ports:'; then
            found_published=1
        fi
    done < "${ROOT_DIR}/docker-compose.yml"

    if [[ "${found_published}" -eq 0 ]]; then
        pass "aats-signer has no 'ports:' section — no inbound network from host."
    else
        fail "aats-signer appears to have a 'ports:' section. The signer must have NO published ports (ADR-0009)."
    fi
}

# ── 8. RPC reachability stub (DNS only, not a real call) ────────────────────
check_rpc_dns() {
    echo -e "\n${BOLD}[8] RPC/Geyser endpoint DNS stub${RESET}"

    # We only do a DNS check here — not a live RPC call, not a real connection.
    # A live RPC benchmark is the operator's responsibility (deploy/colocation-rpc-plan.md).

    local rpc_primary="${RPC_PRIMARY:-}"
    local geyser="${GEYSER_ENDPOINT:-}"

    if [[ -z "${rpc_primary}" ]] || [[ "${rpc_primary}" == *"placeholder"* ]]; then
        warn "RPC_PRIMARY is not configured (placeholder or unset). The bot will not connect to Solana in live modes. Set this before deploying."
    else
        # Extract hostname
        local host
        host=$(echo "${rpc_primary}" | sed -E 's|https?://([^/:?]+).*|\1|')
        if command -v getent &>/dev/null; then
            if getent hosts "${host}" &>/dev/null; then
                pass "RPC_PRIMARY host '${host}' resolves via DNS."
            else
                warn "RPC_PRIMARY host '${host}' did not resolve. Check network connectivity or endpoint configuration."
            fi
        elif command -v nslookup &>/dev/null; then
            if nslookup "${host}" &>/dev/null; then
                pass "RPC_PRIMARY host '${host}' resolves via DNS."
            else
                warn "RPC_PRIMARY host '${host}' did not resolve."
            fi
        else
            info "No DNS lookup tool available — skipping RPC DNS check. Verify connectivity on the deploy host."
        fi
    fi

    if [[ -z "${geyser}" ]] || [[ "${geyser}" == *"placeholder"* ]]; then
        warn "GEYSER_ENDPOINT is not configured. Geyser ingestion will not function."
    else
        info "GEYSER_ENDPOINT is set: ${geyser:0:40}..."
    fi
}

# ── 9. AATS_ENV sanity ───────────────────────────────────────────────────────
check_aats_env() {
    echo -e "\n${BOLD}[9] AATS_ENV mode sanity${RESET}"

    local env="${AATS_ENV:-sim}"
    local dry_run="${DRY_RUN_ENABLED:-true}"

    case "${env}" in
        sim|devnet|mainnet-shadow|mainnet-paper)
            pass "AATS_ENV='${env}' is a safe (non-live) environment."
            if [[ "${dry_run}" != "true" ]]; then
                warn "AATS_ENV='${env}' with DRY_RUN_ENABLED=false is unexpected. These environments should always run with DRY_RUN=true."
            fi
            ;;
        mainnet-live)
            if [[ "${dry_run}" == "true" ]]; then
                warn "AATS_ENV='mainnet-live' with DRY_RUN_ENABLED=true — real capital is still disabled (correct). This is a dry-run against mainnet data."
            else
                warn "AATS_ENV='mainnet-live' with DRY_RUN_ENABLED=false — real capital enabled. Ensure R3 pre-live checklist is complete."
            fi
            ;;
        *)
            warn "AATS_ENV='${env}' is not a recognized environment (sim|devnet|mainnet-shadow|mainnet-paper|mainnet-live)."
            ;;
    esac
}

# ── 10. monitoring config presence ──────────────────────────────────────────
check_monitoring_configs() {
    echo -e "\n${BOLD}[10] Monitoring configs${RESET}"

    # Verify Grafana provisioning references the prometheus datasource
    local prom_ds="${ROOT_DIR}/monitoring/grafana/provisioning/datasources/prometheus.yml"
    if [[ -f "${prom_ds}" ]] && grep -q "prometheus" "${prom_ds}"; then
        pass "Grafana prometheus datasource provisioning found."
    else
        warn "Grafana prometheus datasource provisioning missing or does not reference 'prometheus'."
    fi

    # Verify alertmanager has at least one non-null receiver
    local am="${ROOT_DIR}/monitoring/alertmanager/alertmanager.yml"
    if [[ -f "${am}" ]] && grep -q "telegram_configs\|pagerduty_configs\|webhook_configs" "${am}"; then
        pass "Alertmanager has at least one non-null receiver configured."
    else
        warn "Alertmanager does not appear to have any real receiver (telegram/pagerduty/webhook). Alerts will be silently dropped until configured."
    fi
}

# ── Summary ──────────────────────────────────────────────────────────────────
print_summary() {
    echo ""
    echo -e "${BOLD}=== Summary ===${RESET}"
    if [[ "${HARD_FAIL}" -eq 0 ]] && [[ "${WARN_COUNT}" -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}ALL CHECKS PASSED — no warnings, no failures.${RESET}"
        echo -e "${GREEN}You may proceed: docker compose up -d${RESET}"
    elif [[ "${HARD_FAIL}" -eq 0 ]]; then
        echo -e "${YELLOW}${BOLD}ALL HARD CHECKS PASSED — ${WARN_COUNT} warning(s). Review warnings before deploying.${RESET}"
        echo -e "${YELLOW}You may proceed with caution: docker compose up -d${RESET}"
    else
        echo -e "${RED}${BOLD}HARD FAILURES DETECTED — DO NOT start the stack.${RESET}"
        echo -e "${RED}Fix the FAIL items above, then re-run this script.${RESET}"
        exit 1
    fi
    echo ""
    echo -e "${CYAN}One-command deploy: docker compose up -d${RESET}"
    echo -e "${CYAN}Operator surfaces: dashboard → http://localhost:3000  |  Grafana → http://localhost:3001${RESET}"
    echo -e "${CYAN}Control plane API: http://localhost:8787/api/health${RESET}"
    echo ""
}

# ── Main ─────────────────────────────────────────────────────────────────────
print_header
check_dry_run
check_config_files
check_no_secrets
check_compose_config
check_dry_run_in_compose
check_redis_not_published
check_signer_no_published_ports
check_rpc_dns
check_aats_env
check_monitoring_configs
print_summary
