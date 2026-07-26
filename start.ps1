param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8013,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$cli = Join-Path $PSScriptRoot "node_modules\sysu-anything\bin\sysu-anything.js"

if (-not (Test-Path $python)) {
    throw "Python environment is missing. Run .\setup.ps1 first."
}
if (-not (Test-Path $cli)) {
    throw "SYSU-Anything is missing. Run .\setup.ps1 first."
}

$env:SYSU_ANYTHING_CLI = $cli
$env:SYSU_ANYTHING_NODE = "node"
$url = "http://127.0.0.1:$Port"

if (-not $NoBrowser) {
    Start-Process $url
}

Write-Host "Starting Yiwen QA Assistant at $url"
Write-Host "Press Ctrl+C to stop. Login state stays under .state on this computer."
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
