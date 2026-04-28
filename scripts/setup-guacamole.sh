#!/usr/bin/env bash
# scripts/setup-guacamole.sh
#
# One-shot setup for the DOSM Guacamole stack.  Run this once from the repo
# root before `docker-compose up --build`.
#
# What it does:
#   1. Downloads guacamole-auth-json-1.5.5.jar into guacamole/extensions/
#   2. Generates the Postgres init SQL into guacamole/initdb/
#   3. Generates the shared secret via `dosm guacamole keygen`
#   4. Writes .env with GUACAMOLE_DB_PASSWORD and GUACAMOLE_JSON_SECRET_KEY
#
# Prerequisites: docker, dosm (with DOSM_HOME set), curl or wget.

set -euo pipefail

GUAC_VERSION="1.5.5"
JAR_NAME="guacamole-auth-json-${GUAC_VERSION}.jar"
JAR_URL="https://downloads.apache.org/guacamole/${GUAC_VERSION}/binary/${JAR_NAME}"
JAR_PATH="guacamole/extensions/${JAR_NAME}"
INITDB_SQL="guacamole/initdb/001-initdb.sql"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}warn:${NC} $*"; }
error() { echo -e "${RED}error:${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

for cmd in docker; do
    command -v "$cmd" >/dev/null 2>&1 || error "'$cmd' not found — install Docker first."
done

if ! command -v dosm >/dev/null 2>&1; then
    error "'dosm' not found in PATH.  Run: pip install -e . (inside the repo venv)"
fi

if [ -z "${DOSM_HOME:-}" ]; then
    error "DOSM_HOME is not set.  Run: dosm init <path> && export DOSM_HOME=<path>"
fi

# ---------------------------------------------------------------------------
# 1. Download auth-json extension
# ---------------------------------------------------------------------------

mkdir -p guacamole/extensions guacamole/initdb

if [ -f "$JAR_PATH" ]; then
    warn "$JAR_PATH already present — skipping download."
else
    info "Downloading ${JAR_NAME}..."
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$JAR_PATH" "$JAR_URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$JAR_PATH" "$JAR_URL"
    else
        error "curl or wget required to download the extension JAR."
    fi
    info "Saved to $JAR_PATH"
fi

# ---------------------------------------------------------------------------
# 2. Generate Postgres init SQL
# ---------------------------------------------------------------------------

if [ -f "$INITDB_SQL" ]; then
    warn "$INITDB_SQL already present — skipping generation."
else
    info "Generating Guacamole Postgres schema (pulls image if not cached)..."
    docker run --rm "guacamole/guacamole:${GUAC_VERSION}" \
        /opt/guacamole/bin/initdb.sh --postgres \
        > "$INITDB_SQL"
    info "Saved to $INITDB_SQL"
fi

# ---------------------------------------------------------------------------
# 3. Generate secret key
# ---------------------------------------------------------------------------

KEY_FILE="${DOSM_HOME}/config/guacamole.key"

if [ -f "$KEY_FILE" ]; then
    warn "Key file already exists at $KEY_FILE — using existing key."
    dosm guacamole keygen 2>/dev/null || true
else
    info "Generating Guacamole secret key..."
    dosm guacamole keygen
fi

HEX_KEY=$(cat "$KEY_FILE" 2>/dev/null | tr -d '[:space:]')
[ -n "$HEX_KEY" ] || error "Could not read key from $KEY_FILE"

# ---------------------------------------------------------------------------
# 4. Write .env
# ---------------------------------------------------------------------------

if [ -f ".env" ]; then
    warn ".env already exists — not overwriting."
    echo "      Make sure GUACAMOLE_JSON_SECRET_KEY in .env matches:"
    echo "      $HEX_KEY"
else
    if command -v openssl >/dev/null 2>&1; then
        DB_PASS=$(openssl rand -hex 16)
    else
        DB_PASS=$(python3 -c "import secrets; print(secrets.token_hex(16))")
    fi

    cat > .env << EOF
GUACAMOLE_DB_PASSWORD=${DB_PASS}
GUACAMOLE_JSON_SECRET_KEY=${HEX_KEY}
EOF
    info "Wrote .env"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "================================================================"
echo " Setup complete.  Next steps:"
echo "================================================================"
echo ""
echo "  1. Enable Guacamole in \$DOSM_HOME/config.yaml:"
echo ""
echo "       guacamole:"
echo "         enabled: true"
echo "         base_url: http://127.0.0.1:8080/guacamole"
echo ""
echo "  2. Build and start the Guacamole stack:"
echo ""
echo "       docker-compose up -d --build"
echo ""
echo "  3. Restart DOSM so it picks up the config change:"
echo ""
echo "       dosm serve"
echo ""
echo "  The first 'docker-compose up' initialises the Postgres DB."
echo "  Subsequent starts skip init automatically."
echo ""
