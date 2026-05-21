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


_HANDLERS = {
    "host_info": _cmd_host_info,
    "shell": _cmd_shell,
    "open": _cmd_open,
    "list_dir": _cmd_list_dir,
    "read_file": _cmd_read_file,
    "write_file": _cmd_write_file,
    "make_dir": _cmd_make_dir,
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
    result = _execute(cmd, msg.get("args", {}) or {})
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
