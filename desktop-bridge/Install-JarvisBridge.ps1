<#
  Jarvis Desktop Bridge - one-click installer (no admin needed).

  Installs the bridge to %LOCALAPPDATA%\JarvisDesktopBridge, creates its own
  Python venv + dependencies, writes the per-machine config, and registers a
  HIDDEN auto-start at every Windows logon (Startup-folder shortcut). The bridge
  then connects outbound to the LiveKit control room so OpenJarvis-Avengers can
  operate this PC by voice. Always-on: run.bat self-restarts on crash and the
  bridge auto-reconnects on any network drop.

  Re-running is safe (idempotent). Uninstall with Uninstall-JarvisBridge.ps1.
#>
param(
  [string]$Token = "",
  [string]$Machine = "",
  [string]$TokenEndpoint = "https://openjarvis-avengers-ui-production.up.railway.app/api/bridge/token"
)
$ErrorActionPreference = "Stop"
$AppName    = "JarvisDesktopBridge"
$InstallDir = Join-Path $env:LOCALAPPDATA $AppName
$SrcDir     = $PSScriptRoot

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m)  { Write-Host $m -ForegroundColor Red; Read-Host "Press Enter to exit"; exit 1 }

Info "== Jarvis Desktop Bridge installer =="

# 1) Find a usable Python (>=3.10), else try winget, else guide the user. -------
function Find-Python {
  $tries = @(@("py","-3.12"), @("py","-3"), @("python",$null), @("python3",$null))
  foreach ($t in $tries) {
    try {
      $exe = $t[0]; $arg = $t[1]
      if ($arg) { $out = & $exe $arg "-c" "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null }
      else      { $out = & $exe       "-c" "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null }
      if ($LASTEXITCODE -eq 0 -and $out) {
        $mj,$mn = $out.Trim().Split(".")
        if ([int]$mj -eq 3 -and [int]$mn -ge 10) { return @{ exe=$exe; arg=$arg } }
      }
    } catch {}
  }
  return $null
}

$py = Find-Python
if (-not $py) {
  Warn "No Python 3.10+ found. Trying to install it with winget..."
  try {
    & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
  } catch {}
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [System.Environment]::GetEnvironmentVariable("Path","User")
  $py = Find-Python
}
if (-not $py) {
  Die "Couldn't find or install Python 3.10+. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and re-run this installer."
}
$pyExe = $py.exe; $pyArg = $py.arg
Ok ("Using Python: " + $pyExe + " " + $pyArg)

# 2) Machine label + BRIDGE_TOKEN ----------------------------------------------
if (-not $Machine) {
  $guess = if ($env:COMPUTERNAME -match "rog") { "rog" } else { "laptop" }
  $Machine = Read-Host ("Machine label for this PC (laptop / rog) [" + $guess + "]")
  if (-not $Machine) { $Machine = $guess }
}
$Machine = $Machine.Trim().ToLower()

if (-not $Token) {
  $tf = Join-Path $SrcDir "bridge-token.txt"
  if (Test-Path $tf) { $Token = (Get-Content $tf -Raw).Trim() }
}
if (-not $Token) {
  Write-Host ""
  Write-Host "Paste your BRIDGE_TOKEN (the same one the phone bridge uses; from Railway)." -ForegroundColor Yellow
  $Token = (Read-Host "BRIDGE_TOKEN").Trim()
}
if (-not $Token) { Die "No BRIDGE_TOKEN provided - cannot connect. Re-run and paste it." }

# 3) Lay down files ------------------------------------------------------------
Info "Installing to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item (Join-Path $SrcDir "bridge.py")        $InstallDir -Force
Copy-Item (Join-Path $SrcDir "requirements.txt") $InstallDir -Force
Copy-Item (Join-Path $SrcDir "run-hidden.vbs")   $InstallDir -Force

# 4) Per-machine run.bat (config + crash-restart loop) -------------------------
$runBat = @"
@echo off
cd /d "%~dp0"
set LIVEKIT_TOKEN_ENDPOINT=$TokenEndpoint
set BRIDGE_TOKEN=$Token
set JARVIS_MACHINE=$Machine
set JARVIS_BRIDGE_ALLOW_SHELL=1
:loop
"%~dp0venv\Scripts\python.exe" "%~dp0bridge.py"
timeout /t 5 /nobreak >nul
goto loop
"@
Set-Content -Path (Join-Path $InstallDir "run.bat") -Value $runBat -Encoding ASCII

# 5) Build the venv + install deps now (so first boot is instant) --------------
Info "Creating the Python environment + installing dependencies (one minute)..."
$venv = Join-Path $InstallDir "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
  if ($pyArg) { & $pyExe $pyArg "-m" "venv" $venv } else { & $pyExe "-m" "venv" $venv }
}
$venvPy = Join-Path $venv "Scripts\python.exe"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $InstallDir "requirements.txt") --quiet
Ok "Dependencies installed."

# 6) Auto-start at every logon (Startup-folder shortcut, hidden, no admin) ------
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "JarvisDesktopBridge.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath       = "wscript.exe"
$sc.Arguments        = '"' + (Join-Path $InstallDir "run-hidden.vbs") + '"'
$sc.WorkingDirectory = $InstallDir
$sc.WindowStyle      = 7
$sc.Description       = "Jarvis Desktop Bridge (auto-start)"
$sc.Save()
Ok "Auto-start registered (runs hidden at every logon)."

# 7) Start it now --------------------------------------------------------------
Start-Process "wscript.exe" -ArgumentList ('"' + (Join-Path $InstallDir "run-hidden.vbs") + '"') -WorkingDirectory $InstallDir
Ok "Bridge started."

Write-Host ""
Ok  "DONE - this PC ('$Machine') is now voice-controllable and will reconnect on every boot."
Info "Say e.g.  'open Chrome on my $Machine'  /  'turn the brightness up on my $Machine'  /  'take a screenshot on my $Machine'."
Info "To stop/remove it later, run Uninstall-JarvisBridge.ps1."
Read-Host "Press Enter to close"
