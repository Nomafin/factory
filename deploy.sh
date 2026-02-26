#!/usr/bin/env bash
set -euo pipefail

LOCKFILE="/opt/factory/deploy.lock"
LOGFILE="/opt/factory/deploy.log"
FACTORY_DIR="/opt/factory"
ORCH_DIR="/opt/factory/orchestrator"
VENV="/opt/factory/.venv"
API_URL="http://localhost:8100/api"
POLL_INTERVAL=15
POLL_TIMEOUT=2100  # 35 minutes

# stdout is redirected to deploy.log by the caller (api.py Popen)
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

exec 200>"$LOCKFILE"
if ! flock -n 200; then
    log "Another deploy is already running. Waiting for lock..."
    flock 200
fi

log "=== Deploy started ==="

cd "$FACTORY_DIR"

# Pull latest code
BEFORE=$(git rev-parse HEAD)
git pull --ff-only origin main 2>&1
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    log "Already up-to-date at $AFTER. Exiting."
    exit 0
fi

log "Updated $BEFORE -> $AFTER"

# Check if pyproject.toml changed
if git diff --name-only "$BEFORE" "$AFTER" | grep -q "pyproject.toml"; then
    log "pyproject.toml changed, running pip install..."
    "$VENV/bin/pip" install -e "$ORCH_DIR" 2>&1
else
    log "pyproject.toml unchanged, reinstalling editable anyway..."
    "$VENV/bin/pip" install -e "$ORCH_DIR" 2>&1
fi

# Wait for running agents to finish
log "Checking for running agents..."
elapsed=0
while true; do
    agents=$(curl -sf "$API_URL/agents" 2>/dev/null || echo "[]")
    count=$(echo "$agents" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

    if [ "$count" = "0" ]; then
        log "No agents running. Proceeding with restart."
        break
    fi

    if [ "$elapsed" -ge "$POLL_TIMEOUT" ]; then
        log "WARNING: Timed out after ${POLL_TIMEOUT}s waiting for agents. Restarting anyway."
        break
    fi

    log "Waiting for $count agent(s) to finish... (${elapsed}s elapsed)"
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
done

# Restart the service
log "Restarting factory-orchestrator..."
systemctl restart factory-orchestrator

# Wait a moment and verify
sleep 2
if systemctl is-active --quiet factory-orchestrator; then
    log "=== Deploy successful ==="
else
    log "ERROR: factory-orchestrator failed to start!"
    systemctl status factory-orchestrator 2>&1 || true
    exit 1
fi
