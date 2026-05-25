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
# It prompts for:
#   - BRIDGE_TOKEN  (paste from clipboard — value lives in Railway env)
#   - JARVIS_MACHINE label (default: rog)
#
# Prerequisite: Python 3.12 + `py` launcher on PATH.
# (https://www.python.org/downloads/ — tick "Add to PATH" during install).
#
# Note: this script is intentionally wrapped in a function and uses `return`
# (not `exit`) for early-bail paths. When executed via `iex`, a bare `exit`
# would close the entire PowerShell host — `return` only ends this function,
# leaving the prompt alive so the user can see errors.
# ---------------------------------------------------------------------------

function Invoke-JarvisBridgeBootstrap {
    Write-Host ""
    Write-Host "================================================================="
    Write-Host " Jarvis Desktop Bridge - bootstrap"
    Write-Host "================================================================="
    Write-Host ""

    # 1. Find a usable Python 3.12 launcher
    $pyCmd = $null
    foreach ($candidate in @(@('py','-3.12'), @('py','-3'), @('python',$null))) {
        try {
            $args = @()
            if ($candidate[1]) { $args += $candidate[1] }
            $args += '--version'
            $out = & $candidate[0] @args 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match '3\.(\d+)') {
                $minor = [int]$Matches[1]
                if ($minor -ge 10) {
                    $pyCmd = $candidate
                    Write-Host "[OK] $out  (via '$($candidate[0]) $($candidate[1])')"
                    break
                }
            }
        } catch {}
    }
    if (-not $pyCmd) {
        Write-Host ""
        Write-Host "[FAIL] No Python 3.10+ found via 'py -3.12' / 'py -3' / 'python'."
        Write-Host "       Install Python 3.12 from"
        Write-Host "       https://www.python.org/downloads/release/python-3120/"
        Write-Host "       and tick 'Add Python to PATH' during install."
        Write-Host "       Then re-run this command."
        return
    }

    # 2. Prompt for BRIDGE_TOKEN + machine label
    $BridgeToken = Read-Host -Prompt "Paste BRIDGE_TOKEN (from Railway env)"
    if (-not $BridgeToken) {
        Write-Host "[FAIL] BRIDGE_TOKEN cannot be empty."
        return
    }
    $MachineLabel = Read-Host -Prompt "Machine label (Enter for 'rog')"
    if (-not $MachineLabel) { $MachineLabel = 'rog' }
    $MachineLabel = $MachineLabel.Trim().ToLower()

    # 3. Working dir under Downloads
    $workDir = Join-Path $env:USERPROFILE 'Downloads\jarvis-bridge'
    if (-not (Test-Path $workDir)) {
        New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    }
    Set-Location $workDir
    Write-Host "[OK] Working in $workDir"

    # 4. Fetch bridge.py + requirements.txt from this repo
    $base = 'https://raw.githubusercontent.com/gelson12/friday_jarvis2/main/desktop-bridge'
    foreach ($f in @('bridge.py','requirements.txt')) {
        try {
            Invoke-WebRequest -Uri "$base/$f" -OutFile $f -UseBasicParsing -TimeoutSec 30
            Write-Host "[OK] downloaded $f"
        } catch {
            Write-Host "[FAIL] Could not download $f"
            Write-Host "       $_"
            return
        }
    }

    # 5. Write per-PC run.bat
    $endpoint = 'https://fridayjarvis2-production.up.railway.app/api/bridge/token'
    $runbat = @"
@echo off
REM Jarvis Desktop Bridge launcher - per-PC, never committed.
cd /d "%~dp0"
set LIVEKIT_TOKEN_ENDPOINT=$endpoint
set BRIDGE_TOKEN=$BridgeToken
set JARVIS_MACHINE=$MachineLabel
set JARVIS_BRIDGE_ALLOW_SHELL=1
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
        $args = @()
        if ($pyCmd[1]) { $args += $pyCmd[1] }
        $args += @('-m','venv','venv')
        & $pyCmd[0] @args
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[FAIL] venv creation failed."
            return
        }
    }
    Write-Host "[..] installing dependencies (one-time, ~30s)..."
    & "$workDir\venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] pip install failed."
        return
    }
    Write-Host "[OK] dependencies installed"

    # 7. Push env into THIS shell so we can launch bridge.py directly
    $env:LIVEKIT_TOKEN_ENDPOINT  = $endpoint
    $env:BRIDGE_TOKEN            = $BridgeToken
    $env:JARVIS_MACHINE          = $MachineLabel
    $env:JARVIS_BRIDGE_ALLOW_SHELL = '1'

    # 8. Auto-register the boot-start scheduled task (idempotent).
    # The bridge should come back at every logon and auto-restart on
    # crash, with NO manual ceremony. We attempt registration here so
    # the user doesn't have to remember Step 4 in SETUP_NEW_MACHINE.md.
    # Requires admin PS — if not admin, we skip with a clear note +
    # reprint the one-liner so the user can finish manually.
    $isAdmin = $false
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal $identity
        $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {}
    $taskBat = Join-Path $workDir 'run.bat'
    if ($isAdmin) {
        try {
            if (Get-ScheduledTask -TaskName 'JarvisDesktopBridge' -ErrorAction SilentlyContinue) {
                Write-Host "[OK] task 'JarvisDesktopBridge' already exists - re-registering for latest settings"
                Unregister-ScheduledTask -TaskName 'JarvisDesktopBridge' -Confirm:$false
            }
            Register-ScheduledTask -TaskName 'JarvisDesktopBridge' -Force `
                -Action (New-ScheduledTaskAction -Execute 'cmd.exe' `
                    -Argument "/c `"$taskBat`"" `
                    -WorkingDirectory $workDir) `
                -Trigger (New-ScheduledTaskTrigger -AtLogon) `
                -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -RestartCount 5 `
                    -RestartInterval (New-TimeSpan -Minutes 1)) | Out-Null
            Write-Host "[OK] boot-start task registered (AtLogon, auto-restart 5x/1min)"
        } catch {
            Write-Host "[WARN] Could not register the scheduled task: $_"
            Write-Host "       The bridge will still run NOW from this window, but it"
            Write-Host "       won't come back automatically at next logon. Run this"
            Write-Host "       in an admin PowerShell to retro-fit:"
            Write-Host "       (paste the block from SETUP_NEW_MACHINE.md Step 4)"
        }
    } else {
        Write-Host ""
        Write-Host "[INFO] Skipping boot-start task registration - this shell is not"
        Write-Host "       running as administrator. To make the bridge auto-start"
        Write-Host "       at logon, open ADMIN PowerShell and paste:"
        Write-Host ""
        Write-Host "  `$bat=`"$taskBat`""
        Write-Host "  Register-ScheduledTask -TaskName 'JarvisDesktopBridge' -Force ``"
        Write-Host "      -Action (New-ScheduledTaskAction -Execute 'cmd.exe' ``"
        Write-Host "          -Argument `"/c ```"`$bat```"`" ``"
        Write-Host "          -WorkingDirectory (Split-Path `$bat)) ``"
        Write-Host "      -Trigger (New-ScheduledTaskTrigger -AtLogon) ``"
        Write-Host "      -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries ``"
        Write-Host "          -DontStopIfGoingOnBatteries -RestartCount 5 ``"
        Write-Host "          -RestartInterval (New-TimeSpan -Minutes 1))"
        Write-Host ""
    }

    Write-Host ""
    Write-Host "================================================================="
    Write-Host " Starting bridge as desktop-bridge-$MachineLabel"
    Write-Host " Keep this window open. Press Ctrl+C to stop."
    Write-Host "================================================================="
    Write-Host ""
    & "$workDir\venv\Scripts\python.exe" -u bridge.py
}

# Run the bootstrap.
try {
    Invoke-JarvisBridgeBootstrap
} catch {
    Write-Host ""
    Write-Host "[ERROR] Bootstrap aborted: $_"
}

# Always pause at the end so the user can read any output before the window
# disappears (especially important when launched via `iex` from a one-liner).
Write-Host ""
Write-Host "Bridge has exited. Press Enter to close this window..."
[void](Read-Host)
