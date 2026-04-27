#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidFile     = Join-Path $ProjectRoot '.dosm.pid'

if (-not (Test-Path $PidFile)) {
    Write-Warning "No PID file at $PidFile - is DOSM running?"
    exit 1
}

$target = [int](Get-Content $PidFile -Raw).Trim()
$proc   = Get-Process -Id $target -ErrorAction SilentlyContinue

if ($proc) {
    Stop-Process -Id $target -Force
    Remove-Item $PidFile -Force
    Write-Host "Stopped DOSM (PID $target)"
} else {
    Write-Warning "Process $target is not running - cleaning up stale PID file"
    Remove-Item $PidFile -Force
}
