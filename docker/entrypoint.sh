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
public_url = os.environ.get("GUACAMOLE_PUBLIC_URL", "http://localhost:8888/guacamole")
cfg.setdefault("guacamole", {}).update({
    "base_url":            "http://guacamole:8080/guacamole",
    "public_url":          public_url,
    "dosm_reachable_host": "dosm",
    "tunnel_bind_host":    "0.0.0.0",
})

# Point the LLM client at the Ollama container, not localhost.
cfg.setdefault("llm", {})["base_url"] = "http://ollama:11434"
model = os.environ.get("DOSM_LLM_MODEL", "").strip()
if model:
    cfg["llm"]["model"] = model

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
# 5. Pull LLM model into Ollama (non-blocking — DOSM starts immediately)
# ---------------------------------------------------------------------------

LLM_MODEL="${DOSM_LLM_MODEL:-qwen2.5:3b-instruct}"
python3 - "$LLM_MODEL" <<'PY' &
import sys, time, json, httpx

model = sys.argv[1]
base = "http://ollama:11434"

print(f"[ollama] Waiting for Ollama to be ready…", flush=True)
for _ in range(60):
    try:
        httpx.get(f"{base}/api/tags", timeout=2).raise_for_status()
        break
    except Exception:
        time.sleep(2)
else:
    print(f"[ollama] Not reachable after 120s. Pull manually:", flush=True)
    print(f"  docker compose exec ollama ollama pull {model}", flush=True)
    sys.exit(0)

resp = httpx.get(f"{base}/api/tags", timeout=5)
existing = [m["name"] for m in resp.json().get("models", [])]
model_base = model.split(":")[0]
if any(n == model or n.startswith(model_base + ":") for n in existing):
    print(f"[ollama] Model {model!r} already present", flush=True)
    sys.exit(0)

print(f"[ollama] Pulling {model!r} (this may take a few minutes)…", flush=True)
try:
    with httpx.stream("POST", f"{base}/api/pull", json={"model": model}, timeout=None) as r:
        for line in r.iter_lines():
            try:
                d = json.loads(line)
                if d.get("status") == "success":
                    print(f"[ollama] {model!r} ready", flush=True)
                elif d.get("total") and d.get("completed"):
                    pct = int(d["completed"] * 100 / d["total"])
                    print(f"\r[ollama] {d.get('status', 'downloading')} {pct}%", end="", flush=True)
            except Exception:
                pass
except Exception as e:
    print(f"\n[ollama] Pull failed: {e}", flush=True)
    print(f"  docker compose exec ollama ollama pull {model}", flush=True)
PY

# ---------------------------------------------------------------------------
# 6. Start DOSM
# ---------------------------------------------------------------------------

exec dosm serve --host 0.0.0.0
