@echo off
REM Jarvis Desktop Bridge launcher — run on EACH Windows machine.
REM Edit the values below, then double-click this file (or add it to
REM Task Scheduler "At log on" so the bridge starts with Windows).

cd /d "%~dp0"

REM ---- per-machine config -------------------------------------------
set LIVEKIT_URL=wss://jarvis-98rhrfmj.livekit.cloud
set LIVEKIT_API_KEY=PUT_KEY_HERE
set LIVEKIT_API_SECRET=PUT_SECRET_HERE
REM Distinct label per machine — "laptop" on the laptop, "rog" on the ROG:
set JARVIS_MACHINE=laptop
REM Set to 1 to allow the cloud agent to run shell commands here:
set JARVIS_BRIDGE_ALLOW_SHELL=1
REM -------------------------------------------------------------------

if not exist venv (
  python -m venv venv
  call venv\Scripts\activate.bat
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate.bat
)

python bridge.py
pause
