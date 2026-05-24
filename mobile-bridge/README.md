# Jarvis Mobile Bridge — Android APK

Native Android app that joins the same `jarvis-control` LiveKit room your
`desktop-bridge` PCs use, letting Jarvis operate your phone via voice:
read/send SMS, look up contacts, dial, open any installed app (Instagram,
TikTok, Facebook, Messenger, YouTube, Chrome, Spotify, …), install/
uninstall apps, send WhatsApp messages (Click-to-Chat pre-fill), open
URLs in the browser, and report device status.

Sister project to `desktop-bridge/`. Same outbound model: phone connects
to LiveKit Cloud, no inbound tunnel, no public hostname.

## What it can / cannot do

| ✅ Fully automatic | ✅ One-tap (Android security) | ❌ Not possible |
|---|---|---|
| Read SMS | Dial a contact (tap green) | Send Instagram DM |
| Send SMS | Install app (Play Store tap) | Send FB Messenger to personal user |
| List/search contacts | Uninstall app (system tap) | Send TikTok message |
| Open any installed app | Send WhatsApp (tap send) | Post to IG/TikTok/FB |
| Open URL in browser | | Read history of IG/FB/TikTok DMs |
| Device status (battery, network) | | |

Telegram messaging works too — but it's handled entirely on the worker
via the Telegram Bot API, so the phone doesn't have to be on. See
"Telegram setup" below.

## Build the APK

You need Android Studio Hedgehog (2023.1) or newer + JDK 17.

```
cd mobile-bridge
./gradlew assembleDebug   # or in Android Studio: Run > app
```

The unsigned debug APK lands at:
`app/build/outputs/apk/debug/app-debug.apk`

For a signed release build, generate a personal keystore once
(`keytool -genkey -v -keystore jarvis.jks -keyalg RSA -keysize 2048 -validity 10000 -alias jarvis`),
add a `signingConfigs.release { ... }` block to `app/build.gradle.kts`,
re-enable the commented `signingConfig` line, then `./gradlew assembleRelease`.

**This APK will NOT pass Google Play review** — SMS + Contacts
permissions get auto-rejected without a business case. Sideloading via
ADB or "Install from unknown source" is the only path.

## Install on your phone

1. Transfer `app-debug.apk` to the phone (USB / Drive / email).
2. Phone Settings → Apps → tap the file manager → enable "Install
   unknown apps" for whichever installer you use.
3. Tap the APK → Install.
4. Open the app.
5. Fill in:
   - **Token endpoint URL** — the same URL the desktop-bridge uses
     (`LIVEKIT_TOKEN_ENDPOINT` on the worker). It must mint a JWT for a
     mobile-bridge identity.
   - **Bridge token (BRIDGE_TOKEN)** — same as the desktop-bridge.
   - **Machine name** — short, lowercased, no spaces (e.g. `pixel`).
     Jarvis addresses commands with `target: pixel`. Defaults to
     `Build.MODEL` lowercased.
   - **Control room** — default `jarvis-control` (matches
     `desktop-bridge`).
6. Tap **Save settings**.
7. Tap **Grant required permissions** — accept SMS, Contacts, Phone,
   Microphone (LiveKit SDK requires it for the device probe even though
   we publish no audio), Notifications.
8. Tap **Connect**.
9. A persistent notification appears: "Connected to Jarvis."

You can now leave the app — the foreground service keeps the connection
alive as long as the phone is on.

## Test it from Jarvis

Once the persistent notification says "Connected to Jarvis", in a voice
session try:

- *"What phone do I have?"*
- *"Read me my latest text messages."*
- *"Text Dad that I'll be late."*
- *"Call Mum."*
- *"Open Instagram on my phone."*
- *"Open YouTube."*
- *"Install Spotify on my phone."*
- *"Uninstall TikTok from my phone."*
- *"Open John's Instagram"* → opens `instagram.com/john` in the browser.
- *"WhatsApp Dad saying I'm on my way."*
- *"What's my battery?"*

## Telegram setup (worker-side; no phone needed)

1. Open Telegram → search `@BotFather` → `/newbot` → name it → copy the
   token (looks like `1234567890:ABC...`).
2. Have each Telegram contact you want to message send any message to
   your new bot. The bot now knows their `chat_id`. You can grab those
   IDs by visiting
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
3. On the worker's Railway service Variables tab, set:
   - `TELEGRAM_BOT_TOKEN` = `1234567890:ABC...`
   - `TELEGRAM_CONTACTS_JSON` = `{"mum": 123456789, "dad": 987654321}`
     (lowercase names, integer chat IDs)
4. Try: *"Telegram Mum that I'm on my way."*

## Architecture (one screen)

```
Phone (sideloaded APK)                         Worker (Railway)
─────────────────────                          ────────────────
 MainActivity ─┬─► save config                  agent.py / worker.py
               │                                _maybe_handle_mobile
 BridgeService ┘  ┌───►  LiveKit Cloud  ◄───┐
                  │   "jarvis-control"      │  MobileBridge.send()
 LiveKitClient ───┘  topics:                 └─ mobile-cmd topic ──►
                     mobile-cmd   (worker→) ◄────────────────────────
                     mobile-result(phone→)  ──► result futures
                                                        ▲
 CommandRouter ◄── mobile-cmd packet                    │
   │                                                    │
   ├─ SmsHandler        (Telephony.Sms)                 │
   ├─ ContactsHandler   (ContactsContract)              │
   ├─ DialHandler       (ACTION_DIAL)                   │
   ├─ AppHandler        (PackageManager: open/install/  │
   │                     uninstall/list)                │
   ├─ BrowserHandler    (ACTION_VIEW + URL)             │
   ├─ WhatsAppHandler   (wa.me Click-to-Chat URL)       │
   ├─ DeviceStatusHandler (Battery + Network)           │
   └─ HostInfoHandler                                   │
                                                        │
   reply ── mobile-result ───────────────────────────► ─┘
```

## Troubleshooting

- **Notification says "Reconnecting in 30s" forever** — token endpoint
  is unreachable or returning a non-2xx. Check the URL + bridge token
  match what the worker expects.
- **"Your phone bridge isn't connected, sir"** in voice — the worker
  doesn't see a participant with identity `mobile-bridge-<name>` in the
  control room. Confirm the persistent notification shows "Connected to
  Jarvis"; check Logcat for `LiveKitClient` errors.
- **SMS commands return "permission denied"** — Android 12+ tightened
  SMS rules. Open Settings → Apps → Jarvis Mobile Bridge → Permissions
  and confirm SMS is allowed. If your launcher app isn't the default
  SMS app, READ_SMS may be silently denied; consider using
  `RECEIVE_SMS` broadcasts as a fallback (not in v1).
- **Aggressive battery optimisation kills the service** — Xiaomi, Oppo,
  OnePlus, Samsung One UI all do this. Settings → Battery → Battery
  optimisation → Jarvis Mobile Bridge → **Don't optimise**. Pixel and
  stock Android are well-behaved by default.
- **"open_app" can't find a fuzzy name** — call `list_apps` first to
  see what's actually installed and the exact package names.

## Future phases (not in v1)

- **Notification listening** (`NotificationListenerService`) — surface
  incoming Instagram DMs / FB Messenger / TikTok / WhatsApp messages
  AS they arrive (no history). ~1-2 days.
- **Accessibility service** for WhatsApp / Instagram auto-send (no
  manual tap). Fragile + against ToS; deferred.
- **Telegram personal account** via Telethon (worker-side).
- **Multi-device support** — already in the architecture (`target:
  pixel` vs `target: oneplus`); just needs you to install + connect
  more than one phone.
