# scripts/setup-guacamole.ps1
#
# Run once from the repo root before `docker-compose up --build`.
# Prerequisites: Docker Desktop.

$ErrorActionPreference = 'Stop'

$GuacVersion = '1.5.5'
$JarName     = "guacamole-auth-json-$GuacVersion.jar"
$JarUrl      = "https://downloads.apache.org/guacamole/$GuacVersion/binary/$JarName"
$JarPath     = "guacamole\extensions\$JarName"
$InitSql     = "guacamole\initdb\001-initdb.sql"
$KeyFile     = "dosm-home\config\guacamole.key"

function Info  { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "warn: $m" -ForegroundColor Yellow }
function Abort { param($m) Write-Host "error: $m" -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Abort "docker not found. Install Docker Desktop first."
}

# ---------------------------------------------------------------------------
# 1. Generate Guacamole shared secret
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path "dosm-home\config"  | Out-Null
New-Item -ItemType Directory -Force -Path "guacamole\extensions" | Out-Null
New-Item -ItemType Directory -Force -Path "guacamole\initdb"     | Out-Null

if (Test-Path $KeyFile) {
    Warn "Key file already exists at $KeyFile — using existing key."
} else {
    Info "Generating Guacamole shared secret..."
    $bytes = [byte[]]::new(16)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $hex = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    $hex | Out-File -Encoding ascii -NoNewline $KeyFile
    Info "Wrote $KeyFile"
}

$HexKey = (Get-Content $KeyFile -Raw).Trim()

# ---------------------------------------------------------------------------
# 2. Download auth-json extension
# ---------------------------------------------------------------------------

if (Test-Path $JarPath) {
    Warn "$JarPath already present — skipping download."
} else {
    Info "Downloading $JarName..."
    Invoke-WebRequest -Uri $JarUrl -OutFile $JarPath -UseBasicParsing
    Info "Saved to $JarPath"
}

# ---------------------------------------------------------------------------
# 3. Generate Postgres init SQL
# ---------------------------------------------------------------------------

if (Test-Path $InitSql) {
    Warn "$InitSql already present — skipping generation."
} else {
    Info "Generating Guacamole Postgres schema..."
    docker run --rm "guacamole/guacamole:$GuacVersion" `
        /opt/guacamole/bin/initdb.sh --postgres | Out-File -Encoding utf8 $InitSql
    Info "Saved to $InitSql"
}

# ---------------------------------------------------------------------------
# 4. Write .env
# ---------------------------------------------------------------------------

if (Test-Path ".env") {
    Warn ".env already exists — not overwriting."
    Write-Host "      Ensure GUACAMOLE_JSON_SECRET_KEY=$HexKey is set."
} else {
    $Rand = { -join ((48..57) + (97..102) | Get-Random -Count 32 | % { [char]$_ }) }
    $DbPass    = & $Rand
    $AdminPass = -join ((48..57) + (97..102) | Get-Random -Count 24 | % { [char]$_ })

    @"
# DOSM
DOSM_ADMIN_PASSWORD=$AdminPass

# Guacamole
GUACAMOLE_DB_PASSWORD=$DbPass
GUACAMOLE_JSON_SECRET_KEY=$HexKey
"@ | Out-File -Encoding ascii ".env"

    Info "Wrote .env"
    Write-Host ""
    Write-Host "  Admin password: $AdminPass"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " Setup complete.  Start the full stack with:" -ForegroundColor Cyan
Write-Host ""
Write-Host "   docker-compose up --build"
Write-Host ""
Write-Host " DOSM will be at:       http://localhost:8765"
Write-Host " Guacamole will be at:  http://localhost:8080/guacamole"
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
