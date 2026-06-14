<#  Removes the Jarvis Desktop Bridge: stops it, drops the logon auto-start, and
    deletes the install folder. Run with: right-click > Run with PowerShell, or
    powershell -ExecutionPolicy Bypass -File Uninstall-JarvisBridge.ps1  #>
$ErrorActionPreference = "SilentlyContinue"
$AppName    = "JarvisDesktopBridge"
$InstallDir = Join-Path $env:LOCALAPPDATA $AppName
$startup    = [Environment]::GetFolderPath("Startup")

Write-Host "Stopping the bridge..." -ForegroundColor Cyan
# Kill any python running our bridge.py out of the install dir.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*$AppName*bridge.py*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Remove-Item (Join-Path $startup "JarvisDesktopBridge.lnk") -Force
# Legacy Task Scheduler registration, if an older installer made one.
Unregister-ScheduledTask -TaskName $AppName -Confirm:$false -ErrorAction SilentlyContinue

Remove-Item $InstallDir -Recurse -Force
Write-Host "Removed. The bridge will no longer start at logon." -ForegroundColor Green
Read-Host "Press Enter to close"
