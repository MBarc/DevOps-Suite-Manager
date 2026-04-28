# scripts/setup-guacamole.ps1
#
# Run once from the repo root before `docker-compose up --build`.
# Prerequisites: Docker Desktop.

$ErrorActionPreference = 'Stop'

$GuacVersion = '1.5.5'
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

New-Item -ItemType Directory -Force -Path "dosm-home\config" | Out-Null
New-Item -ItemType Directory -Force -Path "guacamole\initdb"  | Out-Null

if (Test-Path $KeyFile) {
    Warn "Key file already exists at $KeyFile -- using existing key."
} else {
    Info "Generating Guacamole shared secret..."
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $bytes = [byte[]]::new(16)
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $hex = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    [System.IO.File]::WriteAllText((Resolve-Path "dosm-home\config").Path + "\guacamole.key", $hex)
    Info "Wrote $KeyFile"
}

$HexKey = (Get-Content $KeyFile -Raw).Trim()

# ---------------------------------------------------------------------------
# 2. Generate Postgres init SQL
# ---------------------------------------------------------------------------

if (Test-Path $InitSql) {
    Warn "$InitSql already present -- skipping generation."
} else {
    Info "Generating Guacamole Postgres schema..."
    docker run --rm "guacamole/guacamole:$GuacVersion" /opt/guacamole/bin/initdb.sh --postgres |
        Out-File -Encoding utf8 $InitSql
    Info "Saved to $InitSql"
}

# ---------------------------------------------------------------------------
# 3. Write .env
# ---------------------------------------------------------------------------

if (Test-Path ".env") {
    Warn ".env already exists -- not overwriting."
    Write-Host "      Ensure GUACAMOLE_JSON_SECRET_KEY=$HexKey is set."
} else {
    $Rand = { -join ((48..57) + (97..102) | Get-Random -Count 32 | ForEach-Object { [char]$_ }) }
    $DbPass    = & $Rand
    $AdminPass = -join ((48..57) + (97..102) | Get-Random -Count 24 | ForEach-Object { [char]$_ })

    $envContent = "# DOSM`nDOSM_ADMIN_PASSWORD=$AdminPass`n`n# Guacamole`nGUACAMOLE_DB_PASSWORD=$DbPass`nGUACAMOLE_JSON_SECRET_KEY=$HexKey`n"
    [System.IO.File]::WriteAllText((Get-Location).Path + "\.env", $envContent, [System.Text.Encoding]::ASCII)

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
