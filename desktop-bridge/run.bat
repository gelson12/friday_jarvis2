@echo off
REM Jarvis Desktop Bridge launcher — run on EACH Windows machine.
REM Edit the values below, then double-click this file (or add it to
REM Task Scheduler "At log on" so the bridge starts with Windows).

cd /d "%~dp0"

REM ---- per-machine config -------------------------------------------
set LIVEKIT_URL=wss://jarvis-98rhrfmj.livekit.cloud
set LIVEKIT_API_KEY=***REMOVED***
set LIVEKIT_API_SECRET=***REMOVED***
REM Distinct label per machine — "laptop" on the laptop, "rog" on the ROG:
set JARVIS_MACHINE=laptop
REM Set to 1 to allow the cloud agent to run shell commands here:
set JARVIS_BRIDGE_ALLOW_SHELL=1
REM -------------------------------------------------------------------

if not exist venv (
  py -3.12 -m venv venv
)
call venv\Scripts\activate.bat
REM Always sync deps — fast when already satisfied, and picks up new
REM ones (e.g. pycaw for volume control) without a manual reinstall.
python -m pip install -q -r requirements.txt

python bridge.py
pause
