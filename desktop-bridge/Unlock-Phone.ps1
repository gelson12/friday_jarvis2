<#
  Jarvis - remote unlock a PIN/password Android lock over Wireless ADB.
  YOUR OWN devices only. Works for PIN / password (it types it). Does NOT work for
  fingerprint (no software can simulate the sensor) or a swipe PATTERN.

  One-time on the phone (see chat for the OPPO steps):
    Settings > Additional Settings > Developer options > Wireless debugging = ON.

  First run (pair once, then connect every time):
    .\Unlock-Phone.ps1 -PairAddr 192.168.1.50:41234 -PairCode 123456 -Addr 192.168.1.50:43210 -Pin 1234
  After that, just:
    .\Unlock-Phone.ps1            (reuses the saved Addr + PIN)
#>
param(
  [string]$Addr = "",        # ip:port from the Wireless-debugging MAIN screen (connect)
  [string]$Pin = "",         # your numeric PIN / password
  [string]$PairAddr = "",    # ip:port from "Pair device with pairing code" (first time only)
  [string]$PairCode = "",    # the 6-digit pairing code (first time only)
  [switch]$Wake              # only wake + swipe, do not type a PIN
)
$ErrorActionPreference = "Stop"
$Base = $env:LOCALAPPDATA; if (-not $Base) { $Base = $env:USERPROFILE }
$Dir = Join-Path $Base "JarvisDesktopBridge"
$Cfg = Join-Path $Dir "adb-unlock.json"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

function Info($m){ Write-Host $m -ForegroundColor Cyan }
function Ok($m){ Write-Host $m -ForegroundColor Green }
function Warn($m){ Write-Host $m -ForegroundColor Yellow }

function Get-Adb {
  $cands = @(
    (Join-Path $Dir "platform-tools\adb.exe"),
    (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"),
    "adb.exe"
  )
  foreach ($c in $cands) {
    try {
      if ($c -eq "adb.exe") { & $c version *> $null; if ($LASTEXITCODE -eq 0) { return "adb.exe" } }
      elseif (Test-Path $c) { return $c }
    } catch {}
  }
  Info "Downloading Android platform-tools (adb) once..."
  $zip = Join-Path $env:TEMP "platform-tools.zip"
  Invoke-WebRequest "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" -OutFile $zip
  Expand-Archive $zip -DestinationPath $Dir -Force
  Remove-Item $zip -Force
  $adb = Join-Path $Dir "platform-tools\adb.exe"
  if (Test-Path $adb) { Ok "adb ready."; return $adb }
  throw "Could not obtain adb."
}
$ADB = Get-Adb

$saved = $null
if (Test-Path $Cfg) { try { $saved = Get-Content $Cfg -Raw | ConvertFrom-Json } catch {} }
if (-not $Addr -and $saved -and $saved.Addr) { $Addr = $saved.Addr }
if (-not $Pin  -and $saved -and $saved.Pin)  { $Pin  = $saved.Pin }
if (-not $Addr) { $Addr = (Read-Host "Phone Wireless-debugging IP:Port (the connect one, e.g. 192.168.1.50:43210)").Trim() }
if (-not $Pin -and -not $Wake) { $Pin = (Read-Host "Phone PIN/password (blank = only wake)").Trim() }

if ($PairAddr -and $PairCode) {
  Info "Pairing with $PairAddr ..."
  $PairCode | & $ADB pair $PairAddr | Write-Host
}

Info "Connecting to $Addr ..."
$c = (& $ADB connect $Addr 2>&1 | Out-String)
Write-Host $c.Trim()
if ($c -notmatch "connected|already") {
  Warn "Connect failed. On the phone: Wireless debugging ON, same Wi-Fi, paired first (-PairAddr/-PairCode)."
  exit 1
}
function Sh($a){ & $ADB -s $Addr shell @a | Out-Null; Start-Sleep -Milliseconds 350 }
Info "Waking + unlocking..."
Sh @("input","keyevent","224")
Sh @("input","keyevent","82")
Sh @("input","swipe","540","1700","540","500","120")
if ($Pin -and -not $Wake) {
  Sh @("input","text",$Pin)
  Sh @("input","keyevent","66")
}
Ok "Done. Check the phone. If it did NOT open: ColorOS may block input on the lock screen, or it is a pattern/fingerprint lock (which this cannot do)."

@{ Addr = $Addr; Pin = $Pin } | ConvertTo-Json | Set-Content -Path $Cfg -Encoding ascii
