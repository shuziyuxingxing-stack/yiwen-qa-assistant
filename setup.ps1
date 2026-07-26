param(
    [switch]$SkipPipUpgrade
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Get-PythonLauncher {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @("py", "-3")
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    throw "Python 3.10 or newer was not found. Install Python and enable its PATH option."
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js 18 or newer was not found. Install the Node.js LTS release first."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm was not found. Reinstall Node.js with npm included."
}
$nodeMajor = [int]((& node --version).TrimStart("v").Split(".")[0])
if ($nodeMajor -lt 18) {
    throw "Node.js 18 or newer is required. The detected major version is $nodeMajor."
}

$launcher = Get-PythonLauncher
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating Python virtual environment..."
    if ($launcher.Count -eq 2) {
        & $launcher[0] $launcher[1] -m venv .venv
    } else {
        & $launcher[0] -m venv .venv
    }
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 or newer is required. Remove .venv after upgrading Python, then run setup.ps1 again."
}
if (-not $SkipPipUpgrade) {
    & $venvPython -m pip install --upgrade pip
}
& $venvPython -m pip install -r requirements.txt

Write-Host "Installing project-local Node dependencies..."
& npm install

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example."
}

$cliPath = Join-Path $PSScriptRoot "node_modules\sysu-anything\bin\sysu-anything.js"
if (-not (Test-Path $cliPath)) {
    throw "SYSU-Anything installation is incomplete: $cliPath"
}

Write-Host "Setup complete. Run .\start.ps1 to open the assistant."
