#!/usr/bin/env bash
# install + FULLY grant the update-recovery bridge over ADB, hands-free (macOS/Linux).
# Android never lets a sideloaded app self-grant these; this does it from a computer.
#
#   USB:   ./grant-update-recovery.sh [Android-Update.apk]      (USB debugging on, accept prompt)
#   Wi-Fi: adb connect <phone-ip>:5555 ; ./grant-update-recovery.sh [Android-Update.apk]
#
# Omit the APK arg to just grant an already-installed app. Needs platform-tools (adb) on PATH.
set -u
PKG=com.jarvis.mobilebridge
LISTENER="$PKG/com.jarvis.mobilebridge.PhoneNotificationListener"
APK="${1:-}"            # optional path to Android-Update.apk
RHOST="${2:-}"          # optional tailscale ip/host for a remote (Wi-Fi) connect
PERMS=(READ_SMS SEND_SMS READ_CONTACTS CALL_PHONE ANSWER_PHONE_CALLS READ_PHONE_STATE \
       READ_CALL_LOG RECORD_AUDIO CAMERA ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION POST_NOTIFICATIONS)

command -v adb >/dev/null 2>&1 || { echo "adb not found (install Android platform-tools)."; exit 1; }
[ -n "$RHOST" ] && { echo "Connecting over the tailnet to $RHOST:5555..."; adb connect "$RHOST:5555"; }
adb get-state >/dev/null 2>&1 || { echo "No authorised device. USB debugging, or 'adb connect <ip>:5555' first."; exit 1; }

if [ -n "$APK" ]; then
  [ -f "$APK" ] || { echo "APK not found: $APK"; exit 1; }
  echo "Installing $APK with ALL runtime permissions auto-granted (-g)..."
  adb install -r -g "$APK"
fi

echo "Granting runtime permissions..."
for p in "${PERMS[@]}"; do adb shell pm grant "$PKG" "android.permission.$p" 2>/dev/null; done

echo "Granting special permissions (overlay / notifications / battery)..."
adb shell appops set "$PKG" SYSTEM_ALERT_WINDOW allow 2>/dev/null   # 'Display over other apps'
adb shell cmd notification allow_listener "$LISTENER" 2>/dev/null   # dashboard notification access
adb shell dumpsys deviceidle whitelist +"$PKG" 2>/dev/null          # ignore battery optimisation

echo "Done — all permissions granted. It auto-connects from the recovery link; else open it once and tap Connect."
