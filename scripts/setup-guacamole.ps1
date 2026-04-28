# scripts/setup-guacamole.ps1
#
# One-shot setup for the DOSM Guacamole stack on Windows.
# Run from the repo root before `docker-compose up --build`.
#
# Prerequisites: Docker Desktop, dosm installed in the active Python env,
#                DOSM_HOME env var set.

$ErrorActionPreference = 'Stop'

$GuacVersion = '1.5.5'
$JarName     = "guacamole-auth-json-$GuacVersion.jar"
$JarUrl      = "https://downloads.apache.org/guacamole/$GuacVersion/binary/$JarName"
$JarPath     = "guacamole\extensions\$JarName"
$InitSql     = "guacamole\initdb\001-initdb.sql"

function Info  { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "warn: $m" -ForegroundColor Yellow }
function Abort { param($m) Write-Host "error: $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Abort "docker not found. Install Docker Desktop first."
}
if (-not (Get-Command dosm -ErrorAction SilentlyContinue)) {
    Abort "dosm not found in PATH. Run: pip install -e . inside the repo venv."
}
if (-not $env:DOSM_HOME) {
    Abort "DOSM_HOME is not set. Run: dosm init <path> and set DOSM_HOME=<path>."
}

# ---------------------------------------------------------------------------
# 1. Download auth-json extension
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path "guacamole\extensions" | Out-Null
New-Item -ItemType Directory -Force -Path "guacamole\initdb"     | Out-Null

if (Test-Path $JarPath) {
    Warn "$JarPath already present — skipping download."
} else {
    Info "Downloading $JarName..."
    Invoke-WebRequest -Uri $JarUrl -OutFile $JarPath -UseBasicParsing
    Info "Saved to $JarPath"
}

# ---------------------------------------------------------------------------
# 2. Generate Postgres init SQL
# ---------------------------------------------------------------------------

if (Test-Path $InitSql) {
    Warn "$InitSql already present — skipping generation."
} else {
    Info "Generating Guacamole Postgres schema (pulls image if not cached)..."
    docker run --rm "guacamole/guacamole:$GuacVersion" `
        /opt/guacamole/bin/initdb.sh --postgres | Out-File -Encoding utf8 $InitSql
    Info "Saved to $InitSql"
}

# ---------------------------------------------------------------------------
# 3. Generate secret key
# ---------------------------------------------------------------------------

$KeyFile = Join-Path $env:DOSM_HOME "config\guacamole.key"

if (Test-Path $KeyFile) {
    Warn "Key file already exists at $KeyFile — using existing key."
    dosm guacamole keygen 2>$null
} else {
    Info "Generating Guacamole secret key..."
    dosm guacamole keygen
}

$HexKey = (Get-Content $KeyFile -Raw).Trim()
if (-not $HexKey) { Abort "Could not read key from $KeyFile" }

# ---------------------------------------------------------------------------
# 4. Write .env
# ---------------------------------------------------------------------------

if (Test-Path ".env") {
    Warn ".env already exists — not overwriting."
    Write-Host "      Make sure GUACAMOLE_JSON_SECRET_KEY in .env matches:"
    Write-Host "      $HexKey"
} else {
    $DbPass = -join ((48..57) + (97..102) | Get-Random -Count 32 | % {[char]$_})
    @"
GUACAMOLE_DB_PASSWORD=$DbPass
GUACAMOLE_JSON_SECRET_KEY=$HexKey
"@ | Out-File -Encoding ascii ".env"
    Info "Wrote .env"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " Setup complete.  Next steps:" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Enable Guacamole in `$DOSM_HOME\config.yaml:"
Write-Host ""
Write-Host "       guacamole:"
Write-Host "         enabled: true"
Write-Host "         base_url: http://127.0.0.1:8080/guacamole"
Write-Host ""
Write-Host "  2. Build and start the Guacamole stack:"
Write-Host ""
Write-Host "       docker-compose up -d --build"
Write-Host ""
Write-Host "  3. Restart DOSM so it picks up the config change:"
Write-Host ""
Write-Host "       dosm serve"
Write-Host ""
Write-Host "  The first 'docker-compose up' initialises the Postgres DB."
Write-Host "  Subsequent starts skip init automatically."
Write-Host ""
