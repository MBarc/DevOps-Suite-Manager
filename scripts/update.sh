#!/usr/bin/env bash
# One-command update for image-based DOSM installs.
# Usage: bash scripts/update.sh
#
# Backs up dosm-home, pulls the latest images, and restarts the stack.
# DB migrations run automatically on DOSM boot — no manual step needed.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}warn:${NC} $*"; }

COMPOSE_FILE="${DOSM_COMPOSE_FILE:-docker-compose.user.yml}"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "error: $COMPOSE_FILE not found. Run from the directory that contains it." >&2
    exit 1
fi

# 1. Backup dosm-home (config, DB, secrets, docs)
BACKUP="dosm-home.bak.$(date +%Y%m%d_%H%M%S)"
info "Backing up dosm-home → $BACKUP"
cp -r dosm-home "$BACKUP"

# 2. Pull latest images
info "Pulling latest images..."
docker compose -f "$COMPOSE_FILE" pull

# 3. Restart — compose only restarts containers whose image changed
info "Restarting updated containers..."
docker compose -f "$COMPOSE_FILE" up -d

info "Done. DOSM is at http://localhost:${DOSM_PORT:-8765}"
warn "Backup saved at ./$BACKUP — delete once you've verified the update."
