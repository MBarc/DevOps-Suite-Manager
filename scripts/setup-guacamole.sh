#!/usr/bin/env bash
# scripts/setup-guacamole.sh
#
# Run once from the repo root before `docker-compose up --build`.
#
# What it does:
#   1. Creates ./dosm-home/config/ and generates the Guacamole shared secret
#      (the DOSM container reads it from the bind-mount on first boot).
#   2. Downloads guacamole-auth-json-1.5.5.jar into guacamole/extensions/.
#   3. Generates the Postgres init SQL into guacamole/initdb/.
#   4. Writes .env with all required variables.
#
# Prerequisites: docker, python3 or openssl.

set -euo pipefail

GUAC_VERSION="1.5.5"
INITDB_SQL="guacamole/initdb/001-initdb.sql"
KEY_FILE="dosm-home/config/guacamole.key"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}warn:${NC} $*"; }
error() { echo -e "${RED}error:${NC} $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || error "docker not found - install Docker first."

# ---------------------------------------------------------------------------
# 1. Generate Guacamole shared secret
# ---------------------------------------------------------------------------

mkdir -p dosm-home/config guacamole/initdb

if [ -f "$KEY_FILE" ]; then
    warn "Key file already exists at $KEY_FILE - using existing key."
else
    info "Generating Guacamole shared secret..."
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 16 > "$KEY_FILE"
    else
        python3 -c "import secrets; print(secrets.token_bytes(16).hex())" > "$KEY_FILE"
    fi
    info "Wrote $KEY_FILE"
fi

HEX_KEY=$(tr -d '[:space:]' < "$KEY_FILE")

# ---------------------------------------------------------------------------
# 2. Generate Postgres init SQL
# ---------------------------------------------------------------------------

if [ -f "$INITDB_SQL" ]; then
    warn "$INITDB_SQL already present - skipping generation."
else
    info "Generating Guacamole Postgres schema..."
    docker run --rm "guacamole/guacamole:${GUAC_VERSION}" \
        /opt/guacamole/bin/initdb.sh --postgres > "$INITDB_SQL"
    info "Saved to $INITDB_SQL"
fi

# ---------------------------------------------------------------------------
# 4. Write .env
# ---------------------------------------------------------------------------

if [ -f ".env" ]; then
    warn ".env already exists - not overwriting."
    echo "      Ensure these values are set:"
    echo "        GUACAMOLE_JSON_SECRET_KEY=${HEX_KEY}"
else
    if command -v openssl >/dev/null 2>&1; then
        DB_PASS=$(openssl rand -hex 16)
        ADMIN_PASS=$(openssl rand -hex 12)
    else
        DB_PASS=$(python3 -c "import secrets; print(secrets.token_hex(16))")
        ADMIN_PASS=$(python3 -c "import secrets; print(secrets.token_hex(12))")
    fi

    cat > .env << EOF
# DOSM
DOSM_ADMIN_PASSWORD=${ADMIN_PASS}

# Guacamole
GUACAMOLE_DB_PASSWORD=${DB_PASS}
GUACAMOLE_JSON_SECRET_KEY=${HEX_KEY}
EOF
    info "Wrote .env"
    echo ""
    echo "  Admin password: ${ADMIN_PASS}"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "================================================================"
echo " Setup complete.  Start the full stack with:"
echo ""
echo "   docker-compose up --build"
echo ""
echo " DOSM will be at:       http://localhost:8765"
echo " Guacamole will be at:  http://localhost:8080/guacamole"
echo "================================================================"
echo ""
