$ErrorActionPreference = "Stop"

$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DesktopApp = Join-Path $BridgeRoot "handoff_desktop.py"

$Py = Get-Command py -ErrorAction SilentlyContinue
if ($Py) {
    & py -3 $DesktopApp @args
    exit $LASTEXITCODE
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) {
    & python $DesktopApp @args
    exit $LASTEXITCODE
}

Write-Error "Python 3 was not found. Install Python 3, then run this launcher again."
exit 1
