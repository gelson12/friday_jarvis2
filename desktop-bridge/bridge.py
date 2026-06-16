"""Jarvis Desktop Bridge — outbound LiveKit executor (Windows).

Lets a cloud-hosted Jarvis (friday_jarvis2 / OpenJarvis) operate THIS
machine — files, folders, apps, shell — with NO inbound tunnel, no
public hostname, no port-forwarding.

How it works
------------
This process connects OUTBOUND to LiveKit Cloud (the same thing your
voice sessions do — works behind any router/NAT) and joins a fixed
control room. The cloud agent publishes a JSON command on the
`desktop-cmd` data topic; this bridge runs it locally and publishes the
result on `desktop-result`. LiveKit Cloud is just the rendezvous — your
PC never accepts an inbound connection.

Multi-machine
-------------
Run one copy on each Windows machine (laptop AND ROG). Give each a
distinct JARVIS_MACHINE label. Both join the same room; a command's
`target` field selects which machine runs it ("laptop", "rog", or
"all"). A bridge ignores commands not addressed to it.

Run (on each machine)
---------------------
    cd desktop-bridge
    python -m venv venv && venv\\Scripts\\activate
    pip install -r requirements.txt
    set LIVEKIT_URL=wss://jarvis-98rhrfmj.livekit.cloud
    set LIVEKIT_API_KEY=<key>
    set LIVEKIT_API_SECRET=<secret>
    set JARVIS_MACHINE=laptop          REM or: rog
    set JARVIS_BRIDGE_ALLOW_SHELL=1    REM opt-in to the shell command
    python bridge.py

Same LIVEKIT_* project as the voice agents. Leave it running (run.bat
keeps it alive / use Task Scheduler for boot-start).

Security: anything the cloud agent sends runs on this machine with your
user privileges. Keep JARVIS_BRIDGE_ALLOW_SHELL off unless you need it.
"""

from __future__ import annotations

import os
import re
import sys
import json
import socket
import asyncio
import logging
import subprocess

from dotenv import load_dotenv
from livekit import rtc, api

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("desktop-bridge")

CONTROL_ROOM = os.environ.get("JARVIS_CONTROL_ROOM", "jarvis-control")
MACHINE = os.environ.get("JARVIS_MACHINE", socket.gethostname()).strip().lower()
ALLOW_SHELL = os.environ.get("JARVIS_BRIDGE_ALLOW_SHELL", "").strip() in (
    "1",
    "true",
    "yes",
)
TOPIC_CMD = "desktop-cmd"
TOPIC_RESULT = "desktop-result"
_MAX_RESULT = 10_000  # keep well under LiveKit's reliable-data ceiling


# ── Local command handlers ───────────────────────────────────────────
def _cmd_host_info(_args: dict) -> dict:
    return {"machine": MACHINE, "hostname": socket.gethostname()}


def _cmd_shell(args: dict) -> dict:
    if not ALLOW_SHELL:
        return {"error": "shell disabled (set JARVIS_BRIDGE_ALLOW_SHELL=1)"}
    command = args.get("command", "")
    if not command:
        return {"error": "no command"}
    try:
        p = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout", 60)),
        )
        return {
            "returncode": p.returncode,
            "stdout": (p.stdout or "")[-_MAX_RESULT:],
            "stderr": (p.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"error": "timed out"}


def _cmd_open(args: dict) -> dict:
    """Open a file / folder / app / URL with the OS default handler."""
    target = args.get("target") or args.get("path") or ""
    if not target:
        return {"error": "no target"}
    # Resolve a spoken folder name ("my downloads") to a real path; app names,
    # URLs and explicit paths pass through unchanged.
    target = _resolve_dir(target)
    try:
        os.startfile(target)  # noqa: S606 — Windows shell-open
        return {"opened": target}
    except Exception as exc:  # noqa: BLE001
        # Fall back to `start` for apps on PATH / protocol handlers.
        try:
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
            return {"opened": target}
        except Exception as exc2:  # noqa: BLE001
            return {"error": f"{exc}; {exc2}"}


def _cmd_list_dir(args: dict) -> dict:
    path = os.path.expandvars(os.path.expanduser(args.get("path", ".")))
    try:
        entries = []
        with os.scandir(path) as it:
            for e in it:
                entries.append(
                    {"name": e.name, "dir": e.is_dir()}
                )
        return {"path": path, "entries": entries[:500]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_read_file(args: dict) -> dict:
    path = os.path.expandvars(os.path.expanduser(args.get("path", "")))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return {"path": path, "content": fh.read(_MAX_RESULT)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_write_file(args: dict) -> dict:
    path = os.path.expandvars(os.path.expanduser(args.get("path", "")))
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(args.get("content", ""))
        return {"path": path, "written": True}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_make_dir(args: dict) -> dict:
    """Create a folder. Either an explicit ``path``, or a ``name`` placed inside
    a spoken ``parent`` folder (default the Desktop) — "make a folder called
    Reports on my desktop"."""
    name = (args.get("name") or "").strip().strip('"').strip("'")
    if name and not args.get("path"):
        parent = _resolve_dir(args.get("parent") or "desktop")
        path = os.path.join(parent, name)
    else:
        path = _resolve_dir(args.get("path", ""))
    if not path:
        return {"error": "no folder name or path"}
    try:
        os.makedirs(path, exist_ok=True)
        return {"path": path, "created": True}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_volume(args: dict) -> dict:
    """Adjust the system master volume.

    args: ``action`` ∈ {up, down, mute, unmute, set}; for 'set' a
    ``level`` 0-100; for up/down an optional ``step`` (0-1 fraction,
    default 0.10). Uses pycaw for precise absolute control, and falls
    back to the OS media keys (keybd_event) when pycaw is not installed.
    """
    action = (args.get("action") or "").strip().lower()
    if action not in ("up", "down", "mute", "unmute", "set"):
        return {"error": f"unknown volume action '{action}'"}

    # Precise path — pycaw gives absolute get/set + explicit mute.
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL, CoInitialize
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        try:
            CoInitialize()  # COM must be init on this thread
        except Exception:  # noqa: BLE001
            pass
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None
        )
        vol = cast(interface, POINTER(IAudioEndpointVolume))
        if action == "mute":
            vol.SetMute(1, None)
            return {"muted": True}
        if action == "unmute":
            vol.SetMute(0, None)
            return {"muted": False}
        current = vol.GetMasterVolumeLevelScalar()  # 0.0 – 1.0
        if action == "set":
            level = max(0, min(100, int(args.get("level", 50))))
            target = level / 100.0
        else:
            step = float(args.get("step", 0.10))
            target = current + step if action == "up" else current - step
        target = max(0.0, min(1.0, target))
        # A level change should be audible — lift any mute first.
        try:
            if vol.GetMute():
                vol.SetMute(0, None)
        except Exception:  # noqa: BLE001
            pass
        vol.SetMasterVolumeLevelScalar(target, None)
        return {"level": round(target * 100)}
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "pycaw volume path unavailable (%s); using media keys", exc
        )

    # Fallback — OS media keys via keybd_event. No dependency, but only
    # relative (~2% per press) and mute is a toggle, not absolute.
    try:
        import ctypes

        if action == "set":
            return {
                "error": "absolute volume needs pycaw "
                "(pip install pycaw comtypes)"
            }
        vk = {"up": 0xAF, "down": 0xAE, "mute": 0xAD, "unmute": 0xAD}[action]
        presses = int(
            args.get("presses", 1 if action in ("mute", "unmute") else 5)
        )
        user32 = ctypes.windll.user32
        for _ in range(presses):
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)  # 2 = KEYEVENTF_KEYUP
        return {"adjusted": action}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# Process names that produce noise in audio-session enumeration but
# never correspond to a thing the user means when they say "mute X".
_AUDIO_SESSION_SKIP = {"audiodg.exe", "system", ""}


def _cmd_audio_sessions(_args: dict) -> dict:
    """Enumerate per-application audio sessions (Windows).

    Returns one entry per process that currently has an audio session,
    each with: pid, process_name, display_name, volume (0-100), muted,
    is_active (the session is actively rendering audio right now).

    The caller (worker-side volume disambiguation) uses this to know
    which apps are playing sound so it can ask "YouTube or Spotify, sir?"
    instead of fumbling the wrong target.
    """
    try:
        from comtypes import CoInitialize
        from pycaw.pycaw import AudioUtilities
    except Exception as exc:  # noqa: BLE001
        return {"error": f"pycaw unavailable: {exc}", "sessions": []}

    try:
        CoInitialize()
    except Exception:  # noqa: BLE001
        pass

    sessions_out: list[dict] = []
    try:
        for session in AudioUtilities.GetAllSessions():
            try:
                proc = session.Process
                if proc is None:
                    continue
                name = (proc.name() or "").strip()
                if name.lower() in _AUDIO_SESSION_SKIP:
                    continue
                # Session State: 0=Inactive, 1=Active, 2=Expired
                try:
                    state = int(getattr(session, "State", 0) or 0)
                except Exception:  # noqa: BLE001
                    state = 0
                if state == 2:
                    continue
                vol_ctrl = session.SimpleAudioVolume
                try:
                    scalar = float(vol_ctrl.GetMasterVolume())
                except Exception:  # noqa: BLE001
                    scalar = 0.0
                try:
                    muted = bool(vol_ctrl.GetMute())
                except Exception:  # noqa: BLE001
                    muted = False
                display = (getattr(session, "DisplayName", "") or "").strip() or name
                sessions_out.append({
                    "pid": int(proc.pid),
                    "process_name": name,
                    "display_name": display,
                    "volume": round(scalar * 100),
                    "muted": muted,
                    "is_active": state == 1,
                })
            except Exception:  # noqa: BLE001
                # One bad session shouldn't kill the whole enumeration.
                continue
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "sessions": []}

    return {"sessions": sessions_out}


def _cmd_app_volume(args: dict) -> dict:
    """Per-application volume control (Windows).

    args:
      process_name — case-insensitive substring match against the
                     audio-session process name ("spotify" matches
                     "Spotify.exe"; "chrome" matches "chrome.exe").
      action       — up | down | mute | unmute | set
      level        — 0-100 for 'set'
      step         — 0.0-1.0 for up/down (default 0.10)

    When the substring matches multiple sessions (e.g. several Chrome
    processes) the action is applied to all of them.
    """
    process_name = (args.get("process_name") or "").strip().lower()
    if not process_name:
        return {"error": "no process_name"}
    action = (args.get("action") or "").strip().lower()
    if action not in ("up", "down", "mute", "unmute", "set"):
        return {"error": f"unknown app_volume action '{action}'"}

    try:
        from comtypes import CoInitialize
        from pycaw.pycaw import AudioUtilities
    except Exception as exc:  # noqa: BLE001
        return {"error": f"pycaw unavailable: {exc}"}

    try:
        CoInitialize()
    except Exception:  # noqa: BLE001
        pass

    step = max(0.0, min(1.0, float(args.get("step", 0.10))))
    try:
        level_int = int(args.get("level", 50))
    except Exception:  # noqa: BLE001
        level_int = 50
    level_int = max(0, min(100, level_int))

    matched: list[dict] = []
    last_level: int | None = None
    last_muted: bool | None = None

    try:
        for session in AudioUtilities.GetAllSessions():
            try:
                proc = session.Process
                if proc is None:
                    continue
                name = (proc.name() or "").strip()
                if not name or process_name not in name.lower():
                    continue
                vol_ctrl = session.SimpleAudioVolume
                if action == "mute":
                    vol_ctrl.SetMute(1, None)
                    last_muted = True
                elif action == "unmute":
                    vol_ctrl.SetMute(0, None)
                    last_muted = False
                else:
                    current = float(vol_ctrl.GetMasterVolume())
                    if action == "set":
                        target = level_int / 100.0
                    elif action == "up":
                        target = current + step
                    else:
                        target = current - step
                    target = max(0.0, min(1.0, target))
                    # A level change should be audible — lift any mute.
                    try:
                        if vol_ctrl.GetMute():
                            vol_ctrl.SetMute(0, None)
                            last_muted = False
                    except Exception:  # noqa: BLE001
                        pass
                    vol_ctrl.SetMasterVolume(target, None)
                    last_level = round(target * 100)
                matched.append({"pid": int(proc.pid), "process_name": name})
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    if not matched:
        return {"error": f"no audio session for '{process_name}'", "affected": 0}

    return {
        "process_name": process_name,
        "affected": len(matched),
        "matched": matched,
        "action": action,
        "level": last_level,
        "muted": last_muted,
    }


def _cmd_system_status(_args: dict) -> dict:
    """CPU %, RAM, primary-disk usage. psutil is the clean path; falls
    back to a PowerShell probe if psutil is not installed."""
    try:
        import psutil

        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.3)
        sys_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\") + "\\"
        disk = psutil.disk_usage(sys_drive)
        return {
            "cpu_percent": round(cpu, 1),
            "ram_used_gb": round(mem.used / (1024 ** 3), 2),
            "ram_total_gb": round(mem.total / (1024 ** 3), 2),
            "ram_percent": round(mem.percent, 1),
            "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            "disk_total_gb": round(disk.total / (1024 ** 3), 2),
            "disk_percent": round(disk.percent, 1),
            "boot_time": psutil.boot_time(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("psutil unavailable (%s); falling back to PowerShell", exc)

    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$d=Get-PSDrive C;"
        "$cpu=(Get-Counter '\\Processor(_Total)\\% Processor Time' "
        "-SampleInterval 1 -MaxSamples 1).CounterSamples[0].CookedValue;"
        "@{cpu_percent=[math]::Round($cpu,1);"
        "ram_used_gb=[math]::Round("
        "($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/1MB,2);"
        "ram_total_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,2);"
        "disk_free_gb=[math]::Round($d.Free/1GB,2);"
        "disk_total_gb=[math]::Round(($d.Free+$d.Used)/1GB,2)}"
        " | ConvertTo-Json -Compress"
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        return json.loads(p.stdout) if p.stdout else {"error": "no output"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_list_processes(args: dict) -> dict:
    """Top processes by RAM or CPU."""
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return {"error": f"psutil missing: {exc}"}
    top = int(args.get("top", 10))
    by = (args.get("by") or "memory").lower()
    procs: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = p.info
            mem = info.get("memory_info")
            procs.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "",
                    "ram_mb": round(mem.rss / (1024 ** 2), 1) if mem else 0,
                }
            )
        except Exception:  # noqa: BLE001
            continue
    procs.sort(key=lambda x: x["ram_mb"], reverse=True)
    return {"by": by, "top": procs[:top], "count": len(procs)}


def _cmd_close_app(args: dict) -> dict:
    """Terminate every process whose name matches ``name`` (case-insensitive,
    `.exe` optional)."""
    name = (args.get("name") or args.get("target") or "").strip()
    if not name:
        return {"error": "no app name"}
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return {"error": f"psutil missing: {exc}"}
    needle = name.lower().removesuffix(".exe")
    killed: list[int] = []
    targets: list = []
    for p in psutil.process_iter(["pid", "name"]):
        n = (p.info.get("name") or "").lower().removesuffix(".exe")
        if needle and (needle == n or needle in n):
            targets.append(p)
    for p in targets:
        try:
            p.terminate()
            killed.append(p.pid)
        except Exception:  # noqa: BLE001
            pass
    try:
        _gone, alive = psutil.wait_procs(targets, timeout=3.0)
        for p in alive:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return {"closed": len(killed), "pids": killed, "name": name}


def _cmd_delete(args: dict) -> dict:
    """Delete a file or folder. Defaults to the Recycle Bin (recoverable).
    ``permanent=True`` deletes irreversibly; the router never opts in for
    voice commands."""
    path = _resolve_dir(args.get("path", ""))
    if not path:
        return {"error": "no path"}
    if not os.path.exists(path):
        return {"error": f"path not found: {path}"}
    if bool(args.get("permanent", False)):
        try:
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            else:
                os.remove(path)
            return {"deleted": path, "to": "permanent"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    # Recycle Bin path.
    try:
        from send2trash import send2trash

        send2trash(path)
        return {"deleted": path, "to": "recycle_bin"}
    except Exception as exc:  # noqa: BLE001
        logger.info("send2trash unavailable (%s); using PowerShell", exc)
    ps_fn = (
        "DeleteFile" if os.path.isfile(path) else "DeleteDirectory"
    )
    ps = (
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{ps_fn}("
        f"'{path}','OnlyErrorDialogs','SendToRecycleBin')"
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode == 0:
            return {"deleted": path, "to": "recycle_bin"}
        return {"error": (p.stderr or "delete failed")[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_empty_recycle_bin(_args: dict) -> dict:
    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True, text=True, timeout=60,
        )
        return {"emptied": True}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# Directories the file scanner steers around — large, noisy, or system.
_SCAN_SKIP_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", ".git", ".vscode",
    "dist", "build", "AppData", "Application Data", "$Recycle.Bin",
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
}


def _scan_walk(root: str):
    """os.walk that skips system/noisy directories so a search stays fast."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not d.startswith("$")
            and d not in _SCAN_SKIP_DIRS
        ]
        yield dirpath, dirnames, filenames


def _cmd_search_files(args: dict) -> dict:
    """Find files whose name contains ``pattern`` (substring, case-insensitive)
    under ``path`` (default: home)."""
    root = os.path.expandvars(os.path.expanduser(args.get("path", "~")))
    pattern = (args.get("pattern") or args.get("name") or "").lower()
    limit = int(args.get("limit", 50))
    if not pattern:
        return {"error": "no pattern"}
    hits: list[str] = []
    try:
        for dirpath, _dn, filenames in _scan_walk(root):
            for name in filenames:
                if pattern in name.lower():
                    hits.append(os.path.join(dirpath, name))
                    if len(hits) >= limit:
                        return {"matches": hits, "truncated": True,
                                "root": root}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "matches": hits, "root": root}
    return {"matches": hits, "truncated": False, "root": root}


def _cmd_move(args: dict) -> dict:
    src = os.path.expandvars(os.path.expanduser(args.get("src", "")))
    dst = os.path.expandvars(os.path.expanduser(args.get("dst", "")))
    if not (src and dst):
        return {"error": "src and dst are required"}
    try:
        import shutil
        shutil.move(src, dst)
        return {"moved": src, "to": dst}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_copy(args: dict) -> dict:
    src = os.path.expandvars(os.path.expanduser(args.get("src", "")))
    dst = os.path.expandvars(os.path.expanduser(args.get("dst", "")))
    if not (src and dst):
        return {"error": "src and dst are required"}
    try:
        import shutil
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return {"copied": src, "to": dst}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_media_key(args: dict) -> dict:
    """Tap a media transport key — play/pause/next/prev/stop."""
    key = (args.get("key") or "").strip().lower().replace("-", "_")
    vk_map = {
        "play_pause": 0xB3, "play": 0xB3, "pause": 0xB3,
        "next": 0xB0, "previous": 0xB1, "prev": 0xB1,
        "stop": 0xB2,
    }
    vk = vk_map.get(key)
    if vk is None:
        return {"error": f"unknown media key '{key}'"}
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        return {"key": key}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


_MEDIA_EXTS = {
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma",
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm",
}


def _cmd_play_media(args: dict) -> dict:
    """Find a song/video by name in Music/Videos and play it. Falls back to
    a YouTube search when nothing local matches."""
    query = (args.get("query") or args.get("target") or "").strip()
    if not query:
        return {"error": "no query"}
    needle = query.lower()
    roots = [
        os.path.expanduser("~/Music"),
        os.path.expanduser("~/Videos"),
        os.path.expanduser("~/Downloads"),
    ]
    extra = args.get("roots")
    if isinstance(extra, list):
        roots = [
            os.path.expandvars(os.path.expanduser(r)) for r in extra
        ] + roots
    matches: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dn, filenames in _scan_walk(root):
            for name in filenames:
                if os.path.splitext(name)[1].lower() not in _MEDIA_EXTS:
                    continue
                if needle in name.lower():
                    matches.append(os.path.join(dirpath, name))
                    if len(matches) >= 25:
                        break
            if len(matches) >= 25:
                break
        if len(matches) >= 25:
            break
    if matches:
        matches.sort(key=lambda p: len(os.path.basename(p)))
        try:
            os.startfile(matches[0])  # noqa: S606 — Windows shell-open
            return {
                "playing": matches[0],
                "alternatives": matches[1:5],
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "found": matches[:5]}
    # Nothing local — fall back to a YouTube search in the default browser.
    try:
        import urllib.parse, webbrowser
        url = (
            "https://www.youtube.com/results?search_query="
            + urllib.parse.quote(query)
        )
        webbrowser.open(url)
        return {"playing": "youtube", "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_lock_workstation(_args: dict) -> dict:
    try:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return {"locked": True}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# Lifts the cap for HTTP proxy responses — GraphQL search results can
# legitimately push past _MAX_RESULT but stay comfortably below LiveKit's
# ~1 MB reliable-data ceiling.
_HTTP_PROXY_MAX = 700_000


def _cmd_http_proxy(args: dict) -> dict:
    """Proxy an HTTP request to a service on THIS machine.

    Lets a cloud-side caller (the OpenJarvis voice worker / backend) talk
    to a service running locally on this PC — OpenCTI on localhost:8080
    being the motivating case — WITHOUT exposing the service to the
    public internet. The request rides the existing LiveKit data
    channel; the response rides back the same way.

    args:
      base_url  — full base URL (default: 'http://localhost:8080')
      method    — GET | POST | PUT | PATCH | DELETE (default GET)
      path      — request path including leading '/'  (default '/')
      headers   — dict of request headers (optional)
      body      — JSON-serialisable body (POST/PUT/PATCH) (optional)
      timeout   — seconds (default 30, max 120)

    Returns:
      {status, headers, body, json?}
      `json` set when content-type indicates JSON; `body` is the text.
    """
    try:
        import urllib.request
        import urllib.error
    except Exception as exc:  # noqa: BLE001
        return {"error": f"stdlib urllib unavailable: {exc}"}

    base_url = (args.get("base_url") or "http://localhost:8080").rstrip("/")
    path = args.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    url = base_url + path
    method = (args.get("method") or "GET").upper()
    timeout = min(120.0, float(args.get("timeout", 30.0)))

    headers = dict(args.get("headers") or {})
    body = args.get("body")
    raw: bytes | None = None
    if body is not None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        elif isinstance(body, bytes):
            raw = body
        else:
            return {"error": f"unsupported body type {type(body).__name__}"}

    req = urllib.request.Request(
        url, data=raw, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            data = resp.read(_HTTP_PROXY_MAX + 1)
    except urllib.error.HTTPError as exc:
        # Treat HTTP errors as a normal proxied response — the caller
        # cares about the status, not whether urllib raised.
        status = exc.code
        resp_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
        try:
            data = exc.read(_HTTP_PROXY_MAX + 1)
        except Exception:  # noqa: BLE001
            data = b""
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "url": url}

    truncated = len(data) > _HTTP_PROXY_MAX
    body_bytes = data[:_HTTP_PROXY_MAX]
    text = body_bytes.decode("utf-8", errors="replace")
    out: dict = {
        "status": status,
        "headers": resp_headers,
        "body": text,
        "truncated": truncated,
    }
    ctype = (resp_headers.get("Content-Type") or "").lower()
    if "application/json" in ctype or "json" in ctype:
        try:
            out["json"] = json.loads(text)
        except Exception:  # noqa: BLE001
            pass
    return out


# ── Known-folder resolution (the cloud worker can't know Windows paths) ──
def _resolve_dir(spec: str) -> str:
    """Map a spoken folder name ("my downloads", "the desktop", "documents
    folder") to a real Windows path. Leaves real paths / app names / URLs
    untouched, so it's safe to call from open/make_dir/delete/organize."""
    n = (spec or "").strip().strip('"').strip("'")
    if not n:
        return ""
    home = os.path.expanduser("~")
    low = re.sub(r"\b(my|the|a|an)\b", " ", n.lower())
    low = re.sub(r"\s*(folder|directory|dir)\s*$", "", low).strip()
    known = {
        "downloads": "Downloads", "download": "Downloads",
        "documents": "Documents", "document": "Documents", "docs": "Documents",
        "desktop": "Desktop",
        "pictures": "Pictures", "picture": "Pictures", "photos": "Pictures", "images": "Pictures",
        "music": "Music", "songs": "Music",
        "videos": "Videos", "video": "Videos", "movies": "Videos",
        "home": "", "user": "", "user folder": "",
    }
    if low in known:
        return os.path.join(home, known[low]) if known[low] else home
    return os.path.expandvars(os.path.expanduser(n))


def _cmd_brightness(args: dict) -> dict:
    """Adjust the built-in display brightness via WMI. up/down/set/max/min;
    `level` 0-100 for set; `step` (default 20) for up/down. External monitors
    need DDC/CI and usually won't respond — built-in laptop/ROG panel does."""
    action = (args.get("action") or "").strip().lower()
    current = None
    try:
        cur = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"],
            capture_output=True, text=True, timeout=15)
        line = (cur.stdout or "").strip().splitlines()
        if line:
            current = int(re.sub(r"\D", "", line[0]) or "0")
    except Exception:  # noqa: BLE001
        current = None
    base = current if current is not None else 50
    try:
        step = int(args.get("step", 20))
    except (TypeError, ValueError):
        step = 20
    if action == "set":
        level = max(0, min(100, int(args.get("level", 50))))
    elif action in ("max", "maximum", "full", "brightest"):
        level = 100
    elif action in ("min", "minimum", "dimmest"):
        level = 10
    elif action in ("up", "increase", "raise", "brighter", "brighten"):
        level = min(100, base + step)
    elif action in ("down", "decrease", "lower", "dimmer", "dim", "darken"):
        level = max(0, base - step)
    else:
        return {"error": f"unknown brightness action '{action}'"}
    ps = ("$m=Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods;"
          f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{{Timeout=1;Brightness={level}}}")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15)
        if p.returncode != 0:
            return {"error": ("couldn't set brightness — built-in display only "
                              "(external monitors need their own buttons)")[:300]}
        return {"brightness": level}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


_ORGANIZE_MAP = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".svg", ".tiff", ".ico"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx",
                  ".ppt", ".pptx", ".csv", ".md", ".epub"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".flv"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "Installers": {".exe", ".msi"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".html", ".css",
             ".json", ".xml", ".sh", ".ps1", ".rb", ".go", ".rs"},
}


def _cmd_organize_folder(args: dict) -> dict:
    """Tidy a folder: move loose files into Images/Documents/Audio/Video/… by
    type. Existing category subfolders are left alone, so it's re-runnable."""
    path = _resolve_dir(args.get("path") or args.get("target") or "downloads")
    if not os.path.isdir(path):
        return {"error": f"not a folder: {path}"}
    import shutil
    cats = set(_ORGANIZE_MAP) | {"Other"}
    moved: dict[str, int] = {}
    try:
        for entry in os.listdir(path):
            src = os.path.join(path, entry)
            if not os.path.isfile(src):
                continue
            ext = os.path.splitext(entry)[1].lower()
            cat = next((c for c, exts in _ORGANIZE_MAP.items() if ext in exts), "Other")
            dstdir = os.path.join(path, cat)
            os.makedirs(dstdir, exist_ok=True)
            try:
                shutil.move(src, os.path.join(dstdir, entry))
                moved[cat] = moved.get(cat, 0) + 1
            except Exception:  # noqa: BLE001
                pass
        return {"organized": path, "moved": moved, "total": sum(moved.values())}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_screenshot(args: dict) -> dict:
    """Capture the whole screen to ~/Pictures/Jarvis and return the path."""
    import time as _t
    outdir = os.path.join(os.path.expanduser("~"), "Pictures", "Jarvis")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"screenshot-{_t.strftime('%Y%m%d-%H%M%S')}.png")
    try:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(path)
        if args.get("open"):
            try:
                os.startfile(path)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass
        return {"screenshot": path}
    except Exception:  # noqa: BLE001
        pass
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
          "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
          "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
          "$g=[System.Drawing.Graphics]::FromImage($bmp);"
          "$g.CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
          f"$bmp.Save('{path}');$g.Dispose();$bmp.Dispose();")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=20)
        if os.path.exists(path):
            return {"screenshot": path}
        return {"error": "screenshot failed"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_window(args: dict) -> dict:
    """Manage windows: show_desktop/minimize_all, or minimize/maximize/restore/
    close the active window."""
    action = (args.get("action") or "").strip().lower().replace("-", "_")
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if action in ("show_desktop", "minimize_all", "minimise_all", "desktop"):
            VK_LWIN, D = 0x5B, 0x44
            user32.keybd_event(VK_LWIN, 0, 0, 0)
            user32.keybd_event(D, 0, 0, 0)
            user32.keybd_event(D, 0, 2, 0)
            user32.keybd_event(VK_LWIN, 0, 2, 0)
            return {"window": "show_desktop"}
        hwnd = user32.GetForegroundWindow()
        if action in ("minimize", "minimise"):
            user32.ShowWindow(hwnd, 6); return {"window": "minimized"}
        if action in ("maximize", "maximise"):
            user32.ShowWindow(hwnd, 3); return {"window": "maximized"}
        if action in ("restore",):
            user32.ShowWindow(hwnd, 9); return {"window": "restored"}
        if action in ("close",):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return {"window": "closed"}
        return {"error": f"unknown window action '{action}'"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _cmd_power(args: dict) -> dict:
    """Power state: sleep / shutdown / restart / signout / hibernate / lock.
    The worker confirm-gates the destructive ones before they ever get here."""
    action = (args.get("action") or "").strip().lower().replace("-", "_")
    try:
        if action in ("sleep", "suspend"):
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            return {"power": "sleep"}
        if action in ("shutdown", "power_off", "poweroff", "turn_off"):
            subprocess.Popen(["shutdown", "/s", "/t", "0"]); return {"power": "shutdown"}
        if action in ("restart", "reboot"):
            subprocess.Popen(["shutdown", "/r", "/t", "0"]); return {"power": "restart"}
        if action in ("signout", "sign_out", "logoff", "log_off", "logout"):
            subprocess.Popen(["shutdown", "/l"]); return {"power": "signout"}
        if action in ("hibernate",):
            subprocess.Popen(["shutdown", "/h"]); return {"power": "hibernate"}
        if action in ("lock",):
            import ctypes
            ctypes.windll.user32.LockWorkStation(); return {"power": "lock"}
        return {"error": f"unknown power action '{action}'"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# ── Android phone control over ADB ───────────────────────────────────
# The cloud worker can't reach the phone, and the bridge APP can't bypass a secure
# keyguard (no Android API exists). But THIS laptop — on the same hotspot / USB and
# authorised for ADB — can. These handlers are how Jarvis unlocks the phone's pattern
# and grants silent screen-capture. OWNER'S OWN DEVICES ONLY.
_BRIDGE_PKG = "com.jarvis.mobilebridge"
_DEFAULT_PATTERN = os.environ.get("PHONE_PATTERN", "321478965").strip()


def _adb_path() -> str | None:
    """Locate adb.exe — the one the installer dropped, an Android SDK, or PATH."""
    base = os.environ.get("LOCALAPPDATA", "") or os.environ.get("USERPROFILE", "")
    cands = [
        os.path.join(base, "JarvisDesktopBridge", "platform-tools", "adb.exe"),
        os.path.join(base, "Android", "Sdk", "platform-tools", "adb.exe"),
        "adb",
    ]
    for c in cands:
        if c == "adb":
            try:
                subprocess.run(["adb", "version"], capture_output=True, timeout=8)
                return "adb"
            except Exception:  # noqa: BLE001
                continue
        elif c and os.path.exists(c):
            return c
    return None


def _adb_target(serial: str = "") -> str | None:
    """Resolve which phone to drive: an explicit serial/ip:port, the ADB_PHONE_ADDR env
    (wireless), or the single attached USB device. Auto-`connect`s wireless addresses."""
    adb = _adb_path()
    if not adb:
        return None
    cand = serial or os.environ.get("ADB_PHONE_ADDR", "").strip()
    if cand:
        if ":" in cand:
            try:
                subprocess.run([adb, "connect", cand], capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass
        return cand
    try:
        p = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10)
        devs = [ln.split("\t")[0] for ln in p.stdout.splitlines()[1:] if "\tdevice" in ln]
        return devs[0] if devs else None
    except Exception:  # noqa: BLE001
        return None


def _adb_sh(serial: str, *cmd: str, timeout: int = 15) -> tuple[int, str]:
    adb = _adb_path()
    if not adb:
        return 1, "adb not found on this machine"
    full = [adb] + (["-s", serial] if serial else []) + ["shell", *cmd]
    try:
        p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _phone_locked(serial: str) -> bool | None:
    """Is the keyguard showing (phone locked)? True/False, or None if undeterminable.
    `mKeyguardShowing` (verified reliable on the CPH2637/Android 15) is primary;
    `mDreamingLockscreen` is the fallback."""
    _, out = _adb_sh(serial, "dumpsys", "activity", "activities")
    m = re.search(r"mKeyguardShowing=(true|false)", out)
    if m:
        return m.group(1) == "true"
    _, out2 = _adb_sh(serial, "dumpsys", "window")
    m2 = re.search(r"mDreamingLockscreen=(true|false)", out2)
    if m2:
        return m2.group(1) == "true"
    return None


def _cmd_unlock_phone(args: dict) -> dict:
    """Ensure the phone is UNLOCKED over ADB (idempotent — safe to call before any phone
    action). A secure keyguard can't be bypassed by an app or the cloud — only a trusted
    ADB host. We wake, check the keyguard, and ONLY if it's actually locked do we clear the
    credential with the KNOWN pattern (which authorises the change) + dismiss it. The
    pattern is restored by relock_phone, or immediately if restore=true is passed."""
    pattern = str(args.get("pattern") or _DEFAULT_PATTERN).strip()
    serial = _adb_target(str(args.get("serial") or ""))
    if not serial:
        return {"error": "no phone reachable over ADB, sir — connect it by USB "
                         "(or set ADB_PHONE_ADDR=ip:port for wireless debugging)"}
    _adb_sh(serial, "input", "keyevent", "224")  # wake the screen
    locked = _phone_locked(serial)
    if locked is False:
        return {"unlocked": True, "was_locked": False, "serial": serial}
    rc, out = _adb_sh(serial, "locksettings", "clear", "--old", pattern)
    lo = out.lower()
    if rc != 0 and "success" not in lo and "no password" not in lo:
        return {"error": f"unlock failed: {out[:200]} — check USB-debugging is authorised "
                         "and the pattern is correct", "was_locked": True}
    _adb_sh(serial, "wm", "dismiss-keyguard")
    _adb_sh(serial, "input", "keyevent", "82")
    if str(args.get("restore", "")).lower() in ("1", "true", "yes"):
        _adb_sh(serial, "locksettings", "set-pattern", pattern)
        return {"unlocked": True, "was_locked": True, "pattern_restored": True, "serial": serial}
    return {"unlocked": True, "was_locked": True, "pattern_cleared": True, "serial": serial,
            "note": "pattern temporarily off — say 're-lock my phone' to restore it"}


def _cmd_approve_screen_capture(args: dict) -> dict:
    """Tap the system MediaProjection consent ('Start now') over ADB so screen-share needs
    NO physical interaction. The PROJECT_MEDIA app-op bypass is blocked on ColorOS, so we
    auto-approve the dialog instead: poll the UI, prefer 'Entire screen', then tap Start.
    Falls back to a bottom-right coordinate tap if the dialog can't be dumped."""
    import time as _time
    serial = _adb_target(str(args.get("serial") or ""))
    if not serial:
        return {"error": "no phone reachable over ADB"}

    def _dump() -> str:
        _adb_sh(serial, "uiautomator", "dump", "/sdcard/_jc.xml", timeout=12)
        _, xml = _adb_sh(serial, "cat", "/sdcard/_jc.xml", timeout=10)
        return xml

    def _find(xml: str, pattern_txt: str):
        for node in re.findall(r"<node\b[^>]*?/?>", xml):
            tm = re.search(r'\btext="([^"]*)"', node)
            cm = re.search(r'\bcontent-desc="([^"]*)"', node)
            label = (tm.group(1) if tm else "") + " " + (cm.group(1) if cm else "")
            if not label.strip():
                continue
            if re.search(pattern_txt, label, re.I) and "cancel" not in label.lower():
                bm = re.search(r'\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
                if bm:
                    x1, y1, x2, y2 = map(int, bm.groups())
                    return (x1 + x2) // 2, (y1 + y2) // 2
        return None

    deadline = _time.time() + float(args.get("timeout", 8))
    selected_screen = False
    while _time.time() < deadline:
        xml = _dump()
        if not selected_screen:
            # Android 14+ defaults to 'Single app'; pick whole screen if offered.
            es = _find(xml, r"entire screen|whole screen")
            if es:
                _adb_sh(serial, "input", "tap", str(es[0]), str(es[1]))
                selected_screen = True
                _time.sleep(0.5)
                continue
        btn = _find(xml, r"\bstart now\b|\bstart recording\b|\bstart\b|\ballow\b")
        if btn:
            _adb_sh(serial, "input", "tap", str(btn[0]), str(btn[1]))
            return {"approved": True, "tapped": list(btn), "selected_entire_screen": selected_screen}
        _time.sleep(0.6)
    # Fallback: tap the usual bottom-right "Start" position (proportional to screen size).
    _, sz = _adb_sh(serial, "wm", "size")
    m = re.search(r"(\d+)x(\d+)", sz)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        _adb_sh(serial, "input", "tap", str(int(w * 0.85)), str(int(h * 0.92)))
        return {"approved": "fallback", "tapped": [int(w * 0.85), int(h * 0.92)]}
    return {"approved": False, "error": "consent dialog not found"}


def _cmd_relock_phone(args: dict) -> dict:
    """Restore the phone's pattern lock after an unlock."""
    pattern = str(args.get("pattern") or _DEFAULT_PATTERN).strip()
    serial = _adb_target(str(args.get("serial") or ""))
    if not serial:
        return {"error": "no phone reachable over ADB"}
    rc, out = _adb_sh(serial, "locksettings", "set-pattern", pattern)
    return {"relocked": (rc == 0 or "success" in out.lower()), "serial": serial,
            "detail": out[:200]}


def _cmd_grant_screen_capture(args: dict) -> dict:
    """Grant the PROJECT_MEDIA app-op so the bridge screen-captures with NO consent popup
    (owner's device, one-time via ADB; persists until the app is reinstalled)."""
    pkg = str(args.get("package") or _BRIDGE_PKG)
    serial = _adb_target(str(args.get("serial") or ""))
    if not serial:
        return {"error": "no phone reachable over ADB"}
    _adb_sh(serial, "appops", "set", pkg, "PROJECT_MEDIA", "allow")
    _, chk = _adb_sh(serial, "appops", "get", pkg, "PROJECT_MEDIA")
    return {"granted": "allow" in chk.lower(), "serial": serial, "detail": chk[:200]}


# ── FULLY-AUTOMATED phone screen mirror (laptop captures over ADB → LiveKit track) ──
# The phone's in-app MediaProjection needs a consent tap that can't be automated on
# ColorOS. ADB-level `screencap`, however, captures the screen with NO consent — so the
# laptop grabs frames over ADB and publishes them as a video track to the room. Zero taps.
_ROOM = None          # set in _run_once once connected
_LOOP = None          # the bridge's asyncio loop, for scheduling from handler threads
_screen_state = {"stop": True, "publishing": False}


def _screencap_raw(serial: str) -> bytes:
    adb = _adb_path()
    if not adb:
        return b""
    args = [adb] + (["-s", serial] if serial else []) + ["exec-out", "screencap"]
    try:
        return subprocess.run(args, capture_output=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return b""


def _parse_screencap(data: bytes):
    """Android screencap raw = w,h,format[,colorspace] (uint32 LE) header + RGBA8888."""
    if len(data) < 16:
        return None
    w = int.from_bytes(data[0:4], "little")
    h = int.from_bytes(data[4:8], "little")
    if w <= 0 or h <= 0 or w > 8000 or h > 8000:
        return None
    hdr = 16 if len(data) >= 16 + w * h * 4 else 12
    px = data[hdr:hdr + w * h * 4]
    if len(px) < w * h * 4:
        return None
    return w, h, px


async def _screen_loop(serial: str, fps: int, max_w: int) -> None:
    import numpy as np
    from livekit import rtc
    first = await asyncio.to_thread(_screencap_raw, serial)
    parsed = _parse_screencap(first)
    if not parsed:
        logger.error("screen mirror: first screencap failed")
        _screen_state["publishing"] = False
        return
    w, h, _ = parsed
    scale = min(1.0, max_w / float(w))
    ow = max(2, int(w * scale) // 2 * 2)
    oh = max(2, int(h * scale) // 2 * 2)
    ys = np.linspace(0, h - 1, oh).astype(int)
    xs = np.linspace(0, w - 1, ow).astype(int)
    source = rtc.VideoSource(ow, oh)
    track = rtc.LocalVideoTrack.create_video_track("phone-screen", source)
    await _ROOM.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_SCREENSHARE))
    logger.info("screen mirror publishing %dx%d @ %dfps", ow, oh, fps)
    _screen_state["publishing"] = True
    interval = 1.0 / max(1, fps)
    try:
        while not _screen_state["stop"]:
            data = await asyncio.to_thread(_screencap_raw, serial)
            p = _parse_screencap(data)
            if p:
                cw, ch, px = p
                arr = np.frombuffer(px, dtype=np.uint8).reshape(ch, cw, 4)
                if (cw, ch) != (ow, oh):
                    arr = arr[ys][:, xs]
                frame = rtc.VideoFrame(ow, oh, rtc.VideoBufferType.RGBA, arr.tobytes())
                source.capture_frame(frame)
            await asyncio.sleep(interval)
    finally:
        try:
            await _ROOM.local_participant.unpublish_track(track.sid)
        except Exception:  # noqa: BLE001
            pass
        _screen_state["publishing"] = False
        logger.info("screen mirror stopped")


def _adb_screen_serial() -> str | None:
    """Pick the best transport for the big raw screencap frames: a USB device (fast)
    over a wireless one (a 10MB/frame raw capture times out over Wi-Fi)."""
    adb = _adb_path()
    if not adb:
        return None
    try:
        p = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10)
        devs = [ln.split("\t")[0] for ln in p.stdout.splitlines()[1:] if "\tdevice" in ln]
        usb = [d for d in devs if ":" not in d]
        return usb[0] if usb else (devs[0] if devs else None)
    except Exception:  # noqa: BLE001
        return None


def _cmd_phone_screen_start(args: dict) -> dict:
    """Start mirroring the phone screen to the dashboard over ADB — no consent, no tap.
    Prefers USB (fast); falls back to whatever ADB device is present."""
    serial = str(args.get("serial") or "") or _adb_screen_serial() or _adb_target("")
    if not serial:
        return {"error": "no phone reachable over ADB"}
    if _LOOP is None or _ROOM is None:
        return {"error": "bridge not connected to the room yet"}
    if _screen_state.get("publishing"):
        return {"phone_screen": "already streaming", "serial": serial}
    _screen_state["stop"] = False
    asyncio.run_coroutine_threadsafe(
        _screen_loop(serial, int(args.get("fps", 4) or 4), int(args.get("max_w", 540) or 540)),
        _LOOP,
    )
    return {"phone_screen": "streaming", "serial": serial}


def _cmd_phone_screen_stop(args: dict) -> dict:
    _screen_state["stop"] = True
    return {"phone_screen": "stopping"}


# ── Remote CONTROL of the mirrored screen (click/scroll the dashboard → ADB input) ──
_PHONE_WH: dict = {}


def _phone_wh(serial: str):
    if serial in _PHONE_WH:
        return _PHONE_WH[serial]
    _, out = _adb_sh(serial, "wm", "size")
    m = re.search(r"Override size:\s*(\d+)x(\d+)", out) or re.search(r"(\d+)x(\d+)", out)
    wh = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
    _PHONE_WH[serial] = wh
    return wh


def _cmd_phone_tap(args: dict) -> dict:
    """Tap the phone at normalised coords (nx,ny in 0..1) — maps a dashboard click."""
    serial = _adb_screen_serial() or _adb_target("")
    if not serial:
        return {"error": "no phone over ADB"}
    w, h = _phone_wh(serial)
    x = int(max(0.0, min(1.0, float(args.get("nx", 0.5)))) * w)
    y = int(max(0.0, min(1.0, float(args.get("ny", 0.5)))) * h)
    _adb_sh(serial, "input", "tap", str(x), str(y))
    return {"tapped": [x, y]}


def _cmd_phone_swipe(args: dict) -> dict:
    """Swipe/scroll the phone between two normalised points (drag on the dashboard)."""
    serial = _adb_screen_serial() or _adb_target("")
    if not serial:
        return {"error": "no phone over ADB"}
    w, h = _phone_wh(serial)
    x1 = int(max(0.0, min(1.0, float(args.get("nx1", 0.5)))) * w)
    y1 = int(max(0.0, min(1.0, float(args.get("ny1", 0.5)))) * h)
    x2 = int(max(0.0, min(1.0, float(args.get("nx2", 0.5)))) * w)
    y2 = int(max(0.0, min(1.0, float(args.get("ny2", 0.5)))) * h)
    dur = int(args.get("ms", 180))
    _adb_sh(serial, "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur))
    return {"swiped": [x1, y1, x2, y2]}


def _cmd_phone_key(args: dict) -> dict:
    """Navigation keys for the mirrored phone: back / home / recents / power."""
    serial = _adb_screen_serial() or _adb_target("")
    code = {"back": "4", "home": "3", "recents": "187", "power": "26",
            "enter": "66", "volup": "24", "voldown": "25"}.get(str(args.get("key", "")).lower())
    if not serial or not code:
        return {"error": "no phone over ADB or bad key"}
    _adb_sh(serial, "input", "keyevent", code)
    return {"key": args.get("key")}


def _cmd_phone_text(args: dict) -> dict:
    """Type text into the focused field on the mirrored phone."""
    serial = _adb_screen_serial() or _adb_target("")
    txt = str(args.get("text", ""))
    if not serial or not txt:
        return {"error": "no phone over ADB or empty text"}
    _adb_sh(serial, "input", "text", txt.replace(" ", "%s"))
    return {"typed": txt[:40]}


def _cmd_phone_download_media(args: dict) -> dict:
    """Pull the most recent photos/videos off the phone to the laptop's Downloads\\PhoneMedia
    over ADB (no consent needed). count = how many of the newest items."""
    adb = _adb_path()
    serial = _adb_screen_serial() or _adb_target("")
    if not adb or not serial:
        return {"error": "no phone over ADB"}
    base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    dest = os.path.join(base, "Downloads", "PhoneMedia")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"can't create {dest}: {exc}"}
    count = max(1, min(50, int(args.get("count", 10) or 10)))
    exts = (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".webp", ".heic")
    folders = ["/sdcard/DCIM/Camera", "/sdcard/Pictures/Screenshots",
               "/sdcard/Pictures", "/sdcard/Download", "/sdcard/Movies"]
    # Gather newest-first full paths per folder (toybox `ls -1t`), filter media in Python.
    candidates = []
    for folder in folders:
        # NB: call `ls` directly (NOT `sh -c "...|head"`) — adb shell + sh double-parse the
        # args and `ls` ends up running with none (lists /). Filter + limit in Python.
        _, out = _adb_sh(serial, "ls", "-1t", folder, timeout=15)
        for name in out.splitlines():
            name = name.strip()
            if name and name.lower().endswith(exts):
                candidates.append(f"{folder}/{name}")
                if len(candidates) >= count:
                    break
        if len(candidates) >= count:
            break
    pulled = []
    for src in candidates[:count]:
        try:
            r = subprocess.run([adb, "-s", serial, "pull", src, dest],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and "pulled" in (r.stdout + r.stderr).lower():
                pulled.append(src.rsplit("/", 1)[-1])
        except Exception:  # noqa: BLE001
            continue
    return {"downloaded": len(pulled), "dest": dest, "files": pulled[:10]}


_HANDLERS = {
    "host_info": _cmd_host_info,
    "shell": _cmd_shell,
    "open": _cmd_open,
    "list_dir": _cmd_list_dir,
    "read_file": _cmd_read_file,
    "write_file": _cmd_write_file,
    "make_dir": _cmd_make_dir,
    "volume": _cmd_volume,
    "audio_sessions": _cmd_audio_sessions,
    "app_volume": _cmd_app_volume,
    "system_status": _cmd_system_status,
    "list_processes": _cmd_list_processes,
    "close_app": _cmd_close_app,
    "delete": _cmd_delete,
    "empty_recycle_bin": _cmd_empty_recycle_bin,
    "search_files": _cmd_search_files,
    "move": _cmd_move,
    "copy": _cmd_copy,
    "media_key": _cmd_media_key,
    "play_media": _cmd_play_media,
    "lock_workstation": _cmd_lock_workstation,
    "http_proxy": _cmd_http_proxy,
    # Phase 3 — brightness, folder tidy, screenshot, window + power control.
    "brightness": _cmd_brightness,
    "organize_folder": _cmd_organize_folder,
    "screenshot": _cmd_screenshot,
    "window": _cmd_window,
    "power": _cmd_power,
    # Android phone control over ADB (owner's device): unlock the pattern, re-lock it,
    # and grant silent screen-capture.
    "unlock_phone": _cmd_unlock_phone,
    "relock_phone": _cmd_relock_phone,
    "grant_screen_capture": _cmd_grant_screen_capture,
    "approve_screen_capture": _cmd_approve_screen_capture,
    # Fully-automated phone screen mirror (laptop captures over ADB, no consent/tap).
    "phone_screen_start": _cmd_phone_screen_start,
    "phone_screen_stop": _cmd_phone_screen_stop,
    # Remote control of the mirror — dashboard click/scroll/keys -> ADB input.
    "phone_tap": _cmd_phone_tap,
    "phone_swipe": _cmd_phone_swipe,
    "phone_key": _cmd_phone_key,
    "phone_text": _cmd_phone_text,
    "phone_download_media": _cmd_phone_download_media,
}


def _execute(cmd: str, args: dict) -> dict:
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return {"error": f"unknown command '{cmd}'"}
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("command %s failed", cmd)
        return {"error": str(exc)}


# ── LiveKit connection ───────────────────────────────────────────────
# Two auth modes, preferred order:
#   1. Token-fetch (modern). LIVEKIT_TOKEN_ENDPOINT + BRIDGE_TOKEN are set;
#      the bridge POSTs to the endpoint and receives a fresh JWT signed by
#      the worker's LIVEKIT_API_SECRET. The raw secret never has to live on
#      this PC. Tokens are refetched every reconnect.
#   2. Legacy local-mint (fallback). LIVEKIT_API_KEY + LIVEKIT_API_SECRET
#      are set in env; the bridge mints its own JWT locally. Kept so older
#      run.bat configs keep working until they're migrated.
def _fetch_token_via_http() -> tuple[str, str] | None:
    """Fetch a fresh JWT from the worker's bridge-token endpoint."""
    endpoint = os.environ.get("LIVEKIT_TOKEN_ENDPOINT", "").strip()
    bridge_token = os.environ.get("BRIDGE_TOKEN", "").strip()
    if not (endpoint and bridge_token):
        return None
    try:
        import httpx  # local import keeps legacy mode dep-free
    except ImportError:
        logger.error(
            "httpx not installed — required for LIVEKIT_TOKEN_ENDPOINT mode. "
            "Run `pip install -r requirements.txt`."
        )
        return None
    try:
        r = httpx.post(
            endpoint,
            json={"machine": MACHINE},
            headers={"Authorization": f"Bearer {bridge_token}"},
            timeout=15.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("token endpoint request failed: %s", exc)
        return None
    if r.status_code == 401:
        logger.error("token endpoint rejected our BRIDGE_TOKEN (401) — "
                     "check that BRIDGE_TOKEN matches the value set on Railway")
        return None
    if r.status_code == 503:
        logger.error("token endpoint reports BRIDGE_TOKEN not configured on "
                     "the worker side — set BRIDGE_TOKEN in Railway env vars")
        return None
    try:
        r.raise_for_status()
        data = r.json()
        return str(data["serverUrl"]), str(data["token"])
    except Exception as exc:  # noqa: BLE001
        logger.error("token endpoint response invalid (%s): %s",
                     r.status_code, exc)
        return None


def _mint_token_legacy() -> tuple[str, str] | None:
    """Mint a JWT locally from LIVEKIT_API_KEY + LIVEKIT_API_SECRET (legacy)."""
    url = os.environ.get("LIVEKIT_URL", "").strip()
    key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    # Filter out the post-scrub placeholders so they're not mistaken for keys.
    if "***REMOVED***" in (key, secret) or not (url and key and secret):
        return None
    token = (
        api.AccessToken(key, secret)
        .with_identity(f"desktop-bridge-{MACHINE}")
        .with_name(f"Desktop Bridge ({MACHINE})")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=CONTROL_ROOM,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return url, token


def _get_url_and_token() -> tuple[str, str] | None:
    """Pick the modern HTTP-fetch path if configured, else the legacy mint."""
    fetched = _fetch_token_via_http()
    if fetched is not None:
        return fetched
    legacy = _mint_token_legacy()
    if legacy is not None:
        logger.warning(
            "using legacy LIVEKIT_API_KEY/SECRET auth — "
            "set BRIDGE_TOKEN + LIVEKIT_TOKEN_ENDPOINT to move secrets off this PC"
        )
        return legacy
    return None


async def _run_once() -> None:
    creds = _get_url_and_token()
    if creds is None:
        # Tell main() to retry — likely transient (Railway redeploying, or
        # BRIDGE_TOKEN not yet set on the server side).
        raise RuntimeError("no LiveKit creds available — retrying")
    url, token = creds
    room = rtc.Room()

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:  # noqa: ANN001
        if packet.topic != TOPIC_CMD:
            return
        asyncio.create_task(_handle(room, packet))

    await room.connect(url, token)
    # Expose the room + loop so handlers (which run in a worker thread) can publish the
    # phone-screen video track via run_coroutine_threadsafe.
    global _ROOM, _LOOP
    _ROOM = room
    _LOOP = asyncio.get_running_loop()
    logger.info(
        "connected to room '%s' as desktop-bridge-%s (shell=%s)",
        CONTROL_ROOM,
        MACHINE,
        "on" if ALLOW_SHELL else "off",
    )
    # Stay connected until the room drops.
    disconnected = asyncio.Event()
    room.on("disconnected", lambda *_: disconnected.set())
    await disconnected.wait()
    await room.disconnect()


async def _handle(room: rtc.Room, packet: rtc.DataPacket) -> None:
    try:
        msg = json.loads(bytes(packet.data).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return
    target = str(msg.get("target", "")).lower()
    # Only act on commands addressed to this machine (or broadcast).
    if target not in (MACHINE, "all", "any", ""):
        return
    cmd = msg.get("cmd", "")
    cmd_id = msg.get("id", "")
    logger.info("exec %s %s", cmd, msg.get("args", {}))
    # Run the handler in a thread — file scans and shell calls can take
    # seconds and would otherwise block LiveKit's keepalive on the loop.
    result = await asyncio.to_thread(
        _execute, cmd, msg.get("args", {}) or {}
    )
    reply = json.dumps(
        {"id": cmd_id, "machine": MACHINE, "result": result}
    ).encode("utf-8")
    try:
        await room.local_participant.publish_data(
            reply, reliable=True, topic=TOPIC_RESULT
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to publish result: %s", exc)


async def main() -> None:
    # Two valid configurations: (a) LIVEKIT_TOKEN_ENDPOINT + BRIDGE_TOKEN
    # (modern, no raw secret on this PC), or (b) LIVEKIT_URL +
    # LIVEKIT_API_KEY + LIVEKIT_API_SECRET (legacy). Verify at least one.
    modern = bool(
        os.environ.get("LIVEKIT_TOKEN_ENDPOINT") and os.environ.get("BRIDGE_TOKEN")
    )
    legacy = all(
        os.environ.get(v) and "***REMOVED***" not in os.environ.get(v, "")
        for v in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    )
    if not (modern or legacy):
        logger.error(
            "missing config — set either LIVEKIT_TOKEN_ENDPOINT+BRIDGE_TOKEN "
            "(preferred) or LIVEKIT_URL+LIVEKIT_API_KEY+LIVEKIT_API_SECRET"
        )
        sys.exit(1)
    backoff = 1.0
    while True:
        try:
            await _run_once()
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bridge disconnected (%s); reconnecting in %.0fs",
                exc,
                backoff,
            )
        await asyncio.sleep(min(backoff, 30.0))
        backoff = min(backoff * 2.0, 30.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
