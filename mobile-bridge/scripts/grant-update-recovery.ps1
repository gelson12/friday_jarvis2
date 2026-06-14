<#
  grant-update-recovery.ps1 — install + FULLY grant the update-recovery bridge over ADB,
  hands-free. Android never lets a sideloaded app self-grant these; this does it from a PC
  in one shot, so the phone needs zero permission taps.

  USB (cable):
      1) Phone: Settings > Developer options > USB debugging = ON, plug in, accept the prompt.
      2) ./grant-update-recovery.ps1 -Apk .\Android-Update.apk

  Wi-Fi (remote, no cable):
      1) Phone: Settings > Developer options > Wireless debugging = ON (note the IP:port).
      2) adb connect <phone-ip>:5555
      3) ./grant-update-recovery.ps1 -Apk .\Android-Update.apk

  Already installed? omit -Apk to just grant.
  Needs Android platform-tools (adb) on PATH.
#>
param([string]$Apk = "")

$PKG = "com.jarvis.mobilebridge"
$LISTENER = "$PKG/com.jarvis.mobilebridge.PhoneNotificationListener"
$perms = @(
  "android.permission.READ_SMS", "android.permission.SEND_SMS",
  "android.permission.READ_CONTACTS", "android.permission.CALL_PHONE",
  "android.permission.ANSWER_PHONE_CALLS", "android.permission.READ_PHONE_STATE",
  "android.permission.READ_CALL_LOG", "android.permission.RECORD_AUDIO",
  "android.permission.CAMERA", "android.permission.ACCESS_FINE_LOCATION",
  "android.permission.ACCESS_COARSE_LOCATION", "android.permission.POST_NOTIFICATIONS"
)

if (-not (Get-Command adb -ErrorAction SilentlyContinue)) {
  Write-Host "adb not found. Install Android platform-tools and add it to PATH." -ForegroundColor Red; exit 1
}
& adb get-state 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "No authorised device. USB: enable USB debugging + accept the prompt. Wi-Fi: 'adb connect <ip>:5555' first." -ForegroundColor Red; exit 1
}

if ($Apk -ne "") {
  if (-not (Test-Path $Apk)) { Write-Host "APK not found: $Apk" -ForegroundColor Red; exit 1 }
  Write-Host "Installing $Apk with ALL runtime permissions auto-granted (-g)..." -ForegroundColor Cyan
  & adb install -r -g "$Apk"
}

Write-Host "Granting runtime permissions..." -ForegroundColor Cyan
foreach ($p in $perms) { & adb shell pm grant $PKG $p 2>$null }

Write-Host "Granting special permissions (overlay / notifications / battery)..." -ForegroundColor Cyan
& adb shell appops set $PKG SYSTEM_ALERT_WINDOW allow 2>$null   # 'Display over other apps' — lets the bg bridge launch apps
& adb shell cmd notification allow_listener $LISTENER 2>$null   # dashboard notification access
& adb shell dumpsys deviceidle whitelist +$PKG 2>$null          # ignore battery optimisation

Write-Host "Done — update-recovery has SMS / Contacts / Phone / Camera / Mic / Location / Notifications + overlay." -ForegroundColor Green
Write-Host "It auto-connects from the recovery link; otherwise open it once and tap Connect." -ForegroundColor Green
