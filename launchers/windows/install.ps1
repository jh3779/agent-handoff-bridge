$ErrorActionPreference = "Stop"

$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BridgeScript = Join-Path $BridgeRoot "handoff_bridge.py"

$Py = Get-Command py -ErrorAction SilentlyContinue
if ($Py) {
    & py -3 $BridgeScript check
} else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) {
        Write-Error "Python 3 was not found. Install Python 3, then run this installer again."
        exit 1
    }
    & python $BridgeScript check
}

Write-Host ""
Write-Host "Windows launcher is ready:"
Write-Host (Join-Path $PSScriptRoot "handoff-bridge.cmd")
Write-Host ""
Write-Host "If PowerShell blocks scripts, use the .cmd launcher instead."
