# desktop-bridge/bootstrap-rog.ps1
# ---------------------------------------------------------------------------
# One-shot setup for the Jarvis Desktop Bridge on the ROG (or any second PC).
# Downloads bridge.py + requirements.txt from this repo, writes a run.bat
# tailored for the current machine, creates the venv, installs deps, and
# launches the bridge.
#
# Usage on the target machine (PowerShell, no admin needed):
#
#   iex ((iwr -UseBasicParsing `
#       https://raw.githubusercontent.com/gelson12/friday_jarvis2/main/desktop-bridge/bootstrap-rog.ps1).Content)
#
# It will prompt for:
#   - BRIDGE_TOKEN  (paste from clipboard — the value lives in Railway env)
#   - JARVIS_MACHINE label (default: rog)
#
# Prerequisite: Python 3.12 installed and the `py` launcher on PATH
# (https://www.python.org/downloads/ — check "Add to PATH" during install).
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'

Write-Host ""
Write-Host "================================================================="
Write-Host " Jarvis Desktop Bridge — ROG bootstrap"
Write-Host "================================================================="
Write-Host ""

# 1. Sanity check: Python 3.12 on PATH
$pyOk = $false
try {
    $pyVer = & py -3.12 --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $pyVer -match '3\.12') {
        Write-Host "[OK] $pyVer"
        $pyOk = $true
    }
} catch {}
if (-not $pyOk) {
    Write-Host "[FAIL] Python 3.12 not found via 'py -3.12'."
    Write-Host "       Install it from https://www.python.org/downloads/release/python-3120/"
    Write-Host "       (check 'Add to PATH' during install), then re-run this script."
    exit 1
}

# 2. Prompt for BRIDGE_TOKEN + machine label
$BridgeToken = Read-Host -Prompt "Paste BRIDGE_TOKEN (from Railway env)"
if (-not $BridgeToken) { Write-Host "BRIDGE_TOKEN cannot be empty."; exit 1 }

$MachineLabel = Read-Host -Prompt "Machine label (press Enter for 'rog')"
if (-not $MachineLabel) { $MachineLabel = 'rog' }

# 3. Pick a working directory under Downloads
$workDir = Join-Path $env:USERPROFILE 'Downloads\jarvis-bridge'
if (-not (Test-Path $workDir)) { New-Item -ItemType Directory -Path $workDir -Force | Out-Null }
Set-Location $workDir
Write-Host "[OK] Working in $workDir"

# 4. Fetch the latest bridge.py + requirements.txt from this repo
$base = 'https://raw.githubusercontent.com/gelson12/friday_jarvis2/main/desktop-bridge'
foreach ($f in @('bridge.py','requirements.txt')) {
    Invoke-WebRequest -Uri "$base/$f" -OutFile $f -UseBasicParsing -TimeoutSec 30
    Write-Host "[OK] downloaded $f"
}

# 5. Write a per-PC run.bat (gitignored equivalent — never committed back)
$endpoint = 'https://fridayjarvis2-production.up.railway.app/api/bridge/token'
$runbat = @"
@echo off
REM Jarvis Desktop Bridge launcher — per-PC, never committed.
cd /d "%~dp0"

REM ---- per-machine config -------------------------------------------
set LIVEKIT_TOKEN_ENDPOINT=$endpoint
set BRIDGE_TOKEN=$BridgeToken
set JARVIS_MACHINE=$MachineLabel
set JARVIS_BRIDGE_ALLOW_SHELL=1
REM -------------------------------------------------------------------

if not exist venv (
  py -3.12 -m venv venv
)
call venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt

python bridge.py
pause
"@
Set-Content -Path 'run.bat' -Value $runbat -Encoding ASCII
Write-Host "[OK] wrote run.bat (machine=$MachineLabel)"

# 6. Create venv + install deps
if (-not (Test-Path 'venv')) {
    Write-Host "[..] creating venv..."
    & py -3.12 -m venv venv
}
Write-Host "[..] installing dependencies (one-time, ~30s)..."
& "$workDir\venv\Scripts\python.exe" -m pip install -q -r requirements.txt
Write-Host "[OK] deps installed"

# 7. Launch the bridge
Write-Host ""
Write-Host "================================================================="
Write-Host " Starting bridge as desktop-bridge-$MachineLabel"
Write-Host " Keep this window open. To stop the bridge, press Ctrl+C."
Write-Host "================================================================="
Write-Host ""
& "$workDir\venv\Scripts\python.exe" bridge.py
