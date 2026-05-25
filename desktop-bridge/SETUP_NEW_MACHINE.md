# Setting up Jarvis Desktop Bridge on a new machine

Step-by-step guide to make a new Windows PC controllable by **OpenJarvis** and/or **friday_jarvis2** voice agents — same outcome as the existing laptop / ROG bridges.

After setup, voice commands like:

> "Hey Jarvis, open notepad on the **\<machine\>**"
> "Hey Jarvis, list my downloads folder on the **\<machine\>**"
> "Hey Jarvis, decrease the volume on the **\<machine\>**"
> "Hey Jarvis, system status on the **\<machine\>**"

will work end-to-end.

---

## How it works (30 seconds)

1. A Python process (`bridge.py`) runs on the target PC.
2. On startup, it calls `POST https://fridayjarvis2-production.up.railway.app/api/bridge/token` with a `Bearer BRIDGE_TOKEN`.
3. The endpoint returns a short-lived LiveKit JWT signed by the worker's `LIVEKIT_API_SECRET` (which never has to live on the target PC).
4. The bridge connects to LiveKit's `jarvis-control` room as `desktop-bridge-<machine>`.
5. When voice agents say `"...on the <machine>"`, they publish a JSON command on the `desktop-cmd` topic targeting that machine. The bridge runs it locally and publishes a reply on `desktop-result`.

This means:
- **No raw LiveKit secret on the target PC** — only `BRIDGE_TOKEN`.
- **No port forwarding / inbound networking** — the bridge connects outbound to LiveKit Cloud, works through any home NAT.
- **Identical setup for every machine** — only the machine label differs.

---

## Prerequisites

| Requirement | Why | How |
|---|---|---|
| Windows 10/11 | Bridge uses Windows-specific APIs (volume, shell, Recycle Bin) | — |
| Python 3.10+ (3.12 preferred) | bridge runtime | https://www.python.org/downloads/ — **tick "Add Python to PATH"** during install |
| PowerShell | needed to run the bootstrap one-liner | Built into Windows |
| Internet access | bridge talks to Railway + LiveKit | — |
| **`BRIDGE_TOKEN` value** | Authenticates the bridge to the worker | See [where to find it](#where-to-find-bridge_token) below |

Optional:
- Administrator PowerShell — only required if you want the auto-start-on-logon task (step 4 below).

---

## Step 1 — Install Python 3.12 (skip if already there)

1. Go to https://www.python.org/downloads/release/python-3120/
2. Download "Windows installer (64-bit)".
3. Run installer — **tick "Add Python to PATH"** at the bottom of the first screen — then **Install Now**.
4. Confirm:
   ```powershell
   py -3.12 --version
   ```
   Should print `Python 3.12.x`.

---

## Step 2 — Run the bootstrap one-liner

Open PowerShell on the new machine and paste:

```powershell
iex ((iwr -UseBasicParsing https://raw.githubusercontent.com/gelson12/friday_jarvis2/main/desktop-bridge/bootstrap-rog.ps1).Content)
```

It will:
1. Verify Python is available.
2. Download `bridge.py` + `requirements.txt` from this repo into `%USERPROFILE%\Downloads\jarvis-bridge\`.
3. Prompt for **`BRIDGE_TOKEN`** — paste from your password manager / chat / Railway dashboard. See [where to find it](#where-to-find-bridge_token).
4. Prompt for **machine label** — short lowercase tag (`rog`, `laptop`, `studio`, `office-pc`, etc.). This is how voice commands address this machine (`"...on the studio"`).
5. Create a Python virtual environment.
6. Install dependencies (~30 s).
7. Launch the bridge.

You'll see the log lines:

```
HTTP Request: POST https://fridayjarvis2-production.up.railway.app/api/bridge/token "HTTP/1.1 200 OK"
connected to room 'jarvis-control' as desktop-bridge-<machine> (shell=on)
```

**Leave that PowerShell window open** — closing it kills the bridge. (See step 4 for auto-start.)

---

## Step 3 — Verify

From any other machine (or browser), open one of:
- **OpenJarvis voice session** (`https://openjarvis-production-92cf.up.railway.app/voice`)
- **friday_jarvis2 voice session** (`https://fridayjarvis2-production.up.railway.app/`)

Say:

> "Hey Jarvis, system status on the **\<machine\>**"

Expected: spoken reply with CPU %, RAM, disk usage of the new machine.

Or:

> "Hey Jarvis, open notepad on the **\<machine\>**"

Expected: notepad opens on the new machine.

---

## Step 4 — Recommended: auto-start the bridge at logon

> **Auto-handled when you ran Step 2 in admin PowerShell.** The bootstrap
> now registers the scheduled task itself if it has admin privileges.
> Run `Get-ScheduledTask -TaskName JarvisDesktopBridge` — if it returns
> a task, you're done. Otherwise (Step 2 wasn't admin), do this:

Open **Administrator: Windows PowerShell** and paste:

```powershell
$bat="$env:USERPROFILE\Downloads\jarvis-bridge\run.bat"; Register-ScheduledTask -TaskName 'JarvisDesktopBridge' -Force -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$bat`"" -WorkingDirectory (Split-Path $bat)) -Trigger (New-ScheduledTaskTrigger -AtLogon) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1))
```

This creates a Windows Scheduled Task `JarvisDesktopBridge` that:
- Runs at every user logon.
- Auto-restarts up to 5× with 1-minute backoff if the bridge crashes.

### Test without rebooting
```powershell
Start-ScheduledTask -TaskName 'JarvisDesktopBridge'
```

### Disable / remove
```powershell
Unregister-ScheduledTask -TaskName 'JarvisDesktopBridge' -Confirm:$false
```

---

## Where to find BRIDGE_TOKEN

The same `BRIDGE_TOKEN` is shared by all bridges — generated once, lives in two places:

1. **Railway** → `friday_jarvis2` service → **Variables** → `BRIDGE_TOKEN`. (Source of truth.)
2. Each PC's `run.bat` (gitignored, never committed). Same value on every PC.

If you don't have the value handy, grab it from Railway. Or check the laptop's existing `run.bat`.

If you need to **rotate** it (suspected leak):
1. Generate a new random string (~48 chars).
2. Update Railway env var `BRIDGE_TOKEN` on `friday_jarvis2` service.
3. Re-run the bootstrap one-liner on every PC (it overwrites `run.bat` with the new value).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| PowerShell window closes immediately after the one-liner | Python not found — script's `exit` killed the host | Install Python 3.12 (tick "Add to PATH"), retry |
| Bridge prints "token endpoint reports BRIDGE_TOKEN not configured" | `BRIDGE_TOKEN` env var not set on `friday_jarvis2` Railway service | Set it in Railway → friday_jarvis2 → Variables |
| Bridge prints "token endpoint rejected our BRIDGE_TOKEN (401)" | Values mismatch between this PC's `run.bat` and Railway env | Re-run bootstrap on this PC with the correct value |
| `failed to connect... 429 connection minutes limit exceeded` | LiveKit Cloud monthly quota exhausted | Migrate to self-host LiveKit OSS (see [LiveKit-self-host-OSS](https://github.com/gelson12/LiveKit-self-host-OSS)) or wait for monthly reset |
| Bridge logs say "connected" but voice commands don't reach it | The agent worker's regex didn't match the machine name | Use the same lowercase label you set on this PC (e.g. `"on the rog"`, not `"on the gaming PC"`) |
| Bridge keeps reconnecting in a loop | Network / DNS instability | `Clear-DnsClientCache` in PowerShell; check Windows firewall isn't blocking outbound 443 |

---

## File layout after setup

```
%USERPROFILE%\Downloads\jarvis-bridge\
├── bridge.py            # main bridge code (synced from this repo)
├── requirements.txt     # Python deps
├── run.bat              # per-PC config (BRIDGE_TOKEN, machine label, etc.) — gitignored
└── venv\                # Python virtual env with all deps installed
```

Everything else lives in Railway (worker / token endpoint) or in `jarvis-control` (LiveKit room).

---

## Re-deploying / updating an existing PC

If `bridge.py` gets a new feature shipped to `gelson12/friday_jarvis2/main`, just re-run the bootstrap on the PC:

```powershell
iex ((iwr -UseBasicParsing https://raw.githubusercontent.com/gelson12/friday_jarvis2/main/desktop-bridge/bootstrap-rog.ps1).Content)
```

It re-downloads the latest `bridge.py` + `requirements.txt`, rewrites `run.bat` (preserving the BRIDGE_TOKEN you re-enter), and re-installs deps. ~30 seconds.

If auto-start (step 4) is enabled, it'll pick up the new code at next logon — or kill the running bridge python and the scheduled task will relaunch it within a minute.
