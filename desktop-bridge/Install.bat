@echo off
REM Jarvis Desktop Bridge - one-click installer entry point.
REM Double-click this file. No admin needed (installs to your user profile).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-JarvisBridge.ps1"
