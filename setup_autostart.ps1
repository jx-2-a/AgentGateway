# setup_autostart.ps1
# Enable auto-start for Agent Gateway at Windows logon.
# Creates a shortcut to launch_gateway.vbs in the per-user Startup folder,
# so the gateway starts HIDDEN (no black box) after you log in.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File setup_autostart.ps1
# Remove: delete the shortcut it prints below.

$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
$vbs  = Join-Path $repo 'launch_gateway.vbs'
if (-not (Test-Path $vbs)) {
    Write-Host "[ERROR] launch_gateway.vbs not found in $repo" -ForegroundColor Red
    exit 1
}

$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'AgentGateway.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $vbs
$sc.WorkingDirectory = $repo
$sc.Description      = 'Agent Gateway - hidden background auto-start'
$sc.Save()

Write-Host "[OK] Auto-start enabled:" -ForegroundColor Green
Write-Host "     Shortcut : $lnk"
Write-Host "     Target   : $vbs"
Write-Host ""
Write-Host "The gateway will start HIDDEN on next Windows logon."
Write-Host "To disable, delete the shortcut above."
