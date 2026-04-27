#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Venv        = Join-Path $ProjectRoot '.venv'
$PidFile     = Join-Path $ProjectRoot '.dosm.pid'
$LogFile     = Join-Path $ProjectRoot 'dosm.log'
$ErrFile     = Join-Path $ProjectRoot 'dosm.err'
$VenvPython  = Join-Path $Venv 'Scripts\python.exe'

# Bootstrap venv if the interpreter is missing
if (-not (Test-Path $VenvPython)) {
    Write-Host "No venv found - creating one..."
    python -m venv $Venv
    Write-Host "Installing dependencies (this may take a minute)..."
    & (Join-Path $Venv 'Scripts\pip.exe') install --quiet -e $ProjectRoot
    Write-Host "Done."
}

# Default DOSM_HOME if not already set
if (-not $env:DOSM_HOME) {
    $env:DOSM_HOME = Join-Path $ProjectRoot '.dosm-home'
}

# Guard against double-start
if (Test-Path $PidFile) {
    $oldPid = [int](Get-Content $PidFile -Raw).Trim()
    $alive  = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($alive) {
        Write-Warning "DOSM is already running (PID $oldPid). Run stop.ps1 first."
        exit 1
    }
    Remove-Item $PidFile -Force
}

Write-Host "Starting DOSM"
Write-Host "  DOSM_HOME : $env:DOSM_HOME"
Write-Host "  Python    : $VenvPython"
Write-Host "  Log       : $LogFile"

$proc = Start-Process `
    -FilePath $VenvPython `
    -ArgumentList '-m', 'dosm', 'serve' `
    -NoNewWindow `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError  $ErrFile `
    -PassThru

$proc.Id | Set-Content $PidFile
Write-Host "Started - PID $($proc.Id)"
Write-Host "Run '.\scripts\stop.ps1' to stop. Logs: $LogFile"
