#!/bin/sh
set -e

DOSM_HOME=${DOSM_HOME:-/dosm-home}

# ---------------------------------------------------------------------------
# 1. First-boot initialisation
# ---------------------------------------------------------------------------

if [ ! -f "$DOSM_HOME/config.yaml" ]; then
    echo "==> Initialising DOSM home at $DOSM_HOME"
    dosm init "$DOSM_HOME"

    # Patch defaults for container networking:
    #   server.host       0.0.0.0 so the port is reachable outside the container
    #   guacamole.*       service-name URLs for server-side calls; localhost for
    #                     the browser iframe (public_url)
    python3 - <<'PY'
import os, yaml, pathlib

home = pathlib.Path(os.environ["DOSM_HOME"])
p = home / "config.yaml"
cfg = yaml.safe_load(p.read_text()) or {}

cfg.setdefault("server", {})["host"] = "0.0.0.0"
cfg.setdefault("guacamole", {}).update({
    "base_url":            "http://guacamole:8080/guacamole",
    "public_url":          "http://localhost:8080/guacamole",
    "dosm_reachable_host": "dosm",
    "tunnel_bind_host":    "0.0.0.0",
})

p.write_text(yaml.safe_dump(cfg, sort_keys=False))
PY
fi

# ---------------------------------------------------------------------------
# 2. DB migrations (idempotent — safe to run every boot)
# ---------------------------------------------------------------------------

dosm db init

# ---------------------------------------------------------------------------
# 3. Admin user (only on first boot; fails silently if already exists)
# ---------------------------------------------------------------------------

if [ -n "${DOSM_ADMIN_PASSWORD:-}" ]; then
    dosm user create admin --password "$DOSM_ADMIN_PASSWORD" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 4. Guacamole shared secret
# ---------------------------------------------------------------------------

KEY_FILE="$DOSM_HOME/config/guacamole.key"
if [ ! -f "$KEY_FILE" ]; then
    dosm guacamole keygen
    echo ""
    echo "================================================================"
    echo " GUACAMOLE KEY GENERATED — add to .env before restarting:"
    echo "   GUACAMOLE_JSON_SECRET_KEY=$(cat "$KEY_FILE")"
    echo "================================================================"
    echo ""
fi

# ---------------------------------------------------------------------------
# 5. Start DOSM
# ---------------------------------------------------------------------------

exec dosm serve --host 0.0.0.0
