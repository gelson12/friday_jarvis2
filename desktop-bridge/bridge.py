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
    path = os.path.expandvars(os.path.expanduser(args.get("path", "")))
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
    path = os.path.expandvars(os.path.expanduser(args.get("path", "")))
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


_HANDLERS = {
    "host_info": _cmd_host_info,
    "shell": _cmd_shell,
    "open": _cmd_open,
    "list_dir": _cmd_list_dir,
    "read_file": _cmd_read_file,
    "write_file": _cmd_write_file,
    "make_dir": _cmd_make_dir,
    "volume": _cmd_volume,
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
def _mint_token() -> str:
    key = os.environ["LIVEKIT_API_KEY"]
    secret = os.environ["LIVEKIT_API_SECRET"]
    return (
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


async def _run_once() -> None:
    url = os.environ["LIVEKIT_URL"]
    room = rtc.Room()

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:  # noqa: ANN001
        if packet.topic != TOPIC_CMD:
            return
        asyncio.create_task(_handle(room, packet))

    await room.connect(url, _mint_token())
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
    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(var):
            logger.error("missing required env var %s", var)
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
