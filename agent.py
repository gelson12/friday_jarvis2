import os
import re
import time
import json
import uuid
import base64
import asyncio
import logging
from dotenv import load_dotenv
from livekit import agents, rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    RoomInputOptions,
    function_tool,
    StopResponse,
)
from livekit.agents import mcp
from livekit.agents.utils.images import encode, EncodeOptions, ResizeOptions
from livekit.plugins import openai, deepgram, google, silero, noise_cancellation
from openai import AsyncOpenAI
from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION

load_dotenv()
logger = logging.getLogger(__name__)


def prewarm(proc: agents.JobProcess):
    """Pre-download VAD model once per worker process to avoid cold-start delay."""
    proc.userdata["vad"] = silero.VAD.load()


# ── Wake / sleep ─────────────────────────────────────────────────────
# The session auto-connects, so the worker is always listening. It stays
# DORMANT (silent, ignores speech) until it hears the wake phrase, and
# returns to dormant on the sleep phrase. Gating happens in
# on_user_turn_completed via `raise StopResponse()` (verified: livekit-
# agents 1.5.8 catches it and skips the turn).
_WAKE_RE = re.compile(r"\b(hey\s+|ok\s+|okay\s+)?friday\b", re.I)
_SLEEP_RE = re.compile(r"\bgood\s?bye,?\s+friday\b", re.I)


# ── Vision (camera + screen-share) ───────────────────────────────────
# On a vision phrase the worker samples ONE frame from the relevant
# video track, asks an OpenRouter vision model to describe it, and
# injects that description into the user's turn — so the text-only
# Hermes LLM answers as if Friday can see.
_VISION_RE = re.compile(
    r"\b("
    r"what do you see|what can you see|can you see|do you see|"
    r"look at (this|that|me|here|the)|take a look|see (this|that)|"
    r"describe (what|this|the|it|my)|read (this|the|my)|"
    r"what(’s| is|'s)? (this|that|on (the|my)|in front)|"
    r"what am i (holding|showing|wearing|pointing|looking)|"
    r"use your eyes|through (the|your) camera|with your camera"
    r")\b",
    re.I,
)
# Keyword routing: which video source does the user mean?
_SCREEN_RE = re.compile(
    r"\b(screen|page|window|tab|desktop|monitor|sharing|shared)\b", re.I
)
_CAMERA_RE = re.compile(
    r"\b(camera|webcam|me\b|my face|the room|wearing|holding)\b", re.I
)

_VISION_MODEL = os.environ.get(
    "OPENJARVIS_VISION_MODEL", "google/gemini-2.0-flash-001"
)
_vision_client: AsyncOpenAI | None = None


def _is_vision_intent(text: str) -> bool:
    return bool(text) and bool(_VISION_RE.search(text))


def _get_vision_client() -> AsyncOpenAI | None:
    """Lazily build the OpenRouter client; None if no key is set."""
    global _vision_client
    if _vision_client is not None:
        return _vision_client
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        logger.warning("vision requested but OPENROUTER_API_KEY is not set")
        return None
    _vision_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key
    )
    return _vision_client


async def _describe_frame(frame: rtc.VideoFrame, what: str) -> str | None:
    """Return a 1-2 sentence description of a video frame, or None.

    ``what`` is 'camera' or 'screen' — used only to steer the prompt.
    """
    client = _get_vision_client()
    if client is None:
        return None
    try:
        jpeg = encode(
            frame,
            EncodeOptions(
                format="JPEG",
                resize_options=ResizeOptions(
                    width=1280, height=1280, strategy="scale_aspect_fit"
                ),
            ),
        )
        b64 = base64.b64encode(jpeg).decode()
        prompt = (
            "Describe what is visible on this shared screen in 1-3 "
            "concrete sentences — focus on the app/window, any text, and "
            "what the user appears to be doing. No preamble."
            if what == "screen"
            else "Describe what is visible in this webcam frame in 1-2 "
            "concrete sentences. Focus on the main subject, any text, "
            "and notable details. No preamble."
        )
        resp = await client.chat.completions.create(
            model=_VISION_MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                    ],
                }
            ],
        )
        desc = (resp.choices[0].message.content or "").strip()
        return desc or None
    except Exception as exc:  # noqa: BLE001
        logger.error("vision describe failed: %s", exc)
        return None


# ── Desktop bridge (operate the user's Windows machines) ─────────────
# A `desktop-bridge` process runs on each Windows machine (laptop, ROG),
# connects OUTBOUND to LiveKit, and sits in the JARVIS_CONTROL_ROOM. We
# join that same room as a second connection, publish a JSON command on
# the `desktop-cmd` topic, and await the matching reply on
# `desktop-result`. No tunnel / public hostname needed — LiveKit Cloud
# is the rendezvous and the PCs are outbound-only.
_CONTROL_ROOM = os.environ.get("JARVIS_CONTROL_ROOM", "jarvis-control")
_TOPIC_CMD = "desktop-cmd"
_TOPIC_RESULT = "desktop-result"


class DesktopBridge:
    """Lazy LiveKit connection to the desktop-bridge control room."""

    def __init__(self) -> None:
        self._room: rtc.Room | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def _ensure(self) -> rtc.Room | None:
        async with self._lock:
            if self._room is not None:
                return self._room
            url = os.environ.get("LIVEKIT_URL", "")
            key = os.environ.get("LIVEKIT_API_KEY", "")
            secret = os.environ.get("LIVEKIT_API_SECRET", "")
            if not (url and key and secret):
                logger.warning("desktop bridge: LIVEKIT_* env not set")
                return None
            token = (
                api.AccessToken(key, secret)
                .with_identity(f"friday-worker-ctl-{uuid.uuid4().hex[:8]}")
                .with_grants(
                    api.VideoGrants(
                        room_join=True,
                        room=_CONTROL_ROOM,
                        can_publish=True,
                        can_subscribe=True,
                        can_publish_data=True,
                    )
                )
                .to_jwt()
            )
            room = rtc.Room()

            @room.on("data_received")
            def _on_data(packet: rtc.DataPacket) -> None:  # noqa: ANN001
                if packet.topic != _TOPIC_RESULT:
                    return
                try:
                    msg = json.loads(bytes(packet.data).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return
                fut = self._pending.pop(msg.get("id", ""), None)
                if fut and not fut.done():
                    fut.set_result(msg)

            await room.connect(url, token)
            self._room = room
            logger.info("desktop bridge: connected to '%s'", _CONTROL_ROOM)
            return room

    async def send(
        self, target: str, cmd: str, args: dict, timeout: float = 30.0
    ) -> dict:
        """Send a command to a machine's bridge; return its result dict."""
        room = await self._ensure()
        if room is None:
            return {"error": "desktop bridge unavailable (LIVEKIT_* unset)"}
        cmd_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = fut
        payload = json.dumps(
            {"id": cmd_id, "target": target, "cmd": cmd, "args": args}
        ).encode("utf-8")
        try:
            await room.local_participant.publish_data(
                payload, reliable=True, topic=_TOPIC_CMD
            )
            msg = await asyncio.wait_for(fut, timeout)
            return msg.get("result", {})
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            return {
                "error": f"no response from the '{target}' machine — is its "
                f"desktop-bridge running?"
            }
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(cmd_id, None)
            return {"error": str(exc)}


def _norm_machine(machine: str) -> str:
    """Map free-form machine words to a bridge label ('laptop'/'rog')."""
    m = (machine or "").strip().lower()
    if "rog" in m:
        return "rog"
    if m in ("all", "both", "every"):
        return "all"
    return "laptop"  # default — covers 'laptop', 'desktop', 'pc', '' etc.


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        self._desktop = DesktopBridge()
        # Wake/sleep: dormant on connect, woken by "Hey Friday".
        self._awake = False
        # Latest frame + capture time per source.
        self._cam_frame: rtc.VideoFrame | None = None
        self._cam_at: float = 0.0
        self._screen_frame: rtc.VideoFrame | None = None
        self._screen_at: float = 0.0
        self._seen_cam = False
        self._seen_screen = False
        self._video_tasks: set[asyncio.Task] = set()
        self._wire_video(room)

    # ── Video capture (camera + screen-share) ────────────────────────
    def _wire_video(self, room: rtc.Room) -> None:
        """Keep the latest frame from the user's camera and screen tracks."""

        async def _consume(track: rtc.VideoTrack, source) -> None:  # noqa: ANN001
            stream = rtc.VideoStream(track)
            is_screen = source == rtc.TrackSource.SOURCE_SCREENSHARE
            logger.info(
                "video: subscribed to %s track",
                "screen-share" if is_screen else "camera",
            )
            try:
                async for ev in stream:
                    if is_screen:
                        self._screen_frame = ev.frame
                        self._screen_at = time.time()
                        if not self._seen_screen:
                            self._seen_screen = True
                            logger.info("video: first screen-share frame received")
                    else:
                        self._cam_frame = ev.frame
                        self._cam_at = time.time()
                        if not self._seen_cam:
                            self._seen_cam = True
                            logger.info("video: first camera frame received")
            finally:
                await stream.aclose()

        def _spawn(track: rtc.VideoTrack, source) -> None:  # noqa: ANN001
            t = asyncio.create_task(_consume(track, source))
            self._video_tasks.add(t)
            t.add_done_callback(self._video_tasks.discard)

        @room.on("track_subscribed")
        def _on_sub(track, pub, participant):  # noqa: ANN001
            if track.kind == rtc.TrackKind.KIND_VIDEO:
                _spawn(track, pub.source)

        # The audio session won't auto-subscribe video — opt in explicitly.
        @room.on("track_published")
        def _on_pub(pub, participant):  # noqa: ANN001
            if pub.kind == rtc.TrackKind.KIND_VIDEO:
                pub.set_subscribed(True)

        for participant in room.remote_participants.values():
            for pub in participant.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_VIDEO:
                    pub.set_subscribed(True)
                    if pub.track is not None:
                        _spawn(pub.track, pub.source)

    def _pick_vision_source(self, text: str):
        """Return (frame, label) for the source the user means, or (None, _).

        Smart-by-keyword: 'screen'/'page'/'window' → screen-share;
        'me'/'camera'/'room' → camera; otherwise the most-recently
        enabled source.
        """
        want_screen = bool(_SCREEN_RE.search(text))
        want_camera = bool(_CAMERA_RE.search(text))
        if want_screen and not want_camera:
            return self._screen_frame, "screen"
        if want_camera and not want_screen:
            return self._cam_frame, "camera"
        # Ambiguous / both / neither keyword → newest live frame wins.
        if self._screen_frame and self._screen_at >= self._cam_at:
            return self._screen_frame, "screen"
        if self._cam_frame:
            return self._cam_frame, "camera"
        return None, "camera"

    # ── Desktop control tools (laptop + ROG) ────────────────────────
    @function_tool
    async def open_on_machine(self, machine: str, target: str) -> str:
        """Open a file, folder, application, or URL on one of the user's
        Windows machines.

        Args:
            machine: Which machine — "laptop" or "rog".
            target: What to open — an app name (e.g. "notepad"), a file
                or folder path, or a URL.
        """
        m = _norm_machine(machine)
        res = await self._desktop.send(m, "open", {"target": target})
        if res.get("error"):
            return f"Could not open {target} on the {m}: {res['error']}"
        return f"Opened {res.get('opened', target)} on the {m}."

    @function_tool
    async def list_files_on_machine(self, machine: str, path: str) -> str:
        """List the files and folders in a directory on a Windows machine.

        Args:
            machine: Which machine — "laptop" or "rog".
            path: Directory path to list (e.g. "C:/Users/Gelson/Downloads").
        """
        m = _norm_machine(machine)
        res = await self._desktop.send(m, "list_dir", {"path": path})
        if res.get("error"):
            return f"Could not list {path} on the {m}: {res['error']}"
        entries = res.get("entries", [])
        names = [
            f"{e['name']}/" if e.get("dir") else e["name"] for e in entries
        ]
        return f"{m} {res.get('path', path)} ({len(names)} items): " + ", ".join(
            names[:60]
        )

    @function_tool
    async def read_file_on_machine(self, machine: str, path: str) -> str:
        """Read a text file from a Windows machine.

        Args:
            machine: Which machine — "laptop" or "rog".
            path: File path to read.
        """
        m = _norm_machine(machine)
        res = await self._desktop.send(m, "read_file", {"path": path})
        if res.get("error"):
            return f"Could not read {path} on the {m}: {res['error']}"
        return f"{path} on the {m}:\n{res.get('content', '')}"

    @function_tool
    async def run_command_on_machine(self, machine: str, command: str) -> str:
        """Run a shell command on a Windows machine and return its output.
        Use only when the user explicitly asks to run something.

        Args:
            machine: Which machine — "laptop" or "rog".
            command: The shell command to run.
        """
        m = _norm_machine(machine)
        res = await self._desktop.send(
            m, "shell", {"command": command}, timeout=70.0
        )
        if res.get("error"):
            return f"Command failed on the {m}: {res['error']}"
        out = (res.get("stdout") or "").strip()
        err = (res.get("stderr") or "").strip()
        rc = res.get("returncode")
        parts = [f"{m} exit {rc}"]
        if out:
            parts.append(f"stdout: {out}")
        if err:
            parts.append(f"stderr: {err}")
        return " | ".join(parts)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Wake/sleep gate, then vision injection.

        The session is always connected, so this fires on every spoken
        turn. While DORMANT we ignore everything except the wake phrase;
        `raise StopResponse()` cleanly drops the turn (no LLM reply).
        """
        text = getattr(new_message, "text_content", "") or ""

        # ── Wake / sleep state machine ───────────────────────────────
        if not self._awake:
            if _WAKE_RE.search(text):
                self._awake = True
                rest = _WAKE_RE.sub("", text).strip(" ,.!?-")
                if len(rest.split()) >= 2:
                    # "Hey Friday, what's the time" — answer the question.
                    new_message.content = [rest]
                    text = rest
                else:
                    await self.session.say("Yes, sir?")
                    raise StopResponse()
            else:
                # Dormant — ignore all non-wake speech silently.
                raise StopResponse()
        else:
            if _SLEEP_RE.search(text):
                self._awake = False
                await self.session.say("Goodbye, sir.")
                raise StopResponse()

        # ── Vision injection (only runs while awake) ─────────────────
        if not _is_vision_intent(text):
            return

        frame, label = self._pick_vision_source(text)
        if frame is None:
            new_message.content.append(
                f"\n\n[Vision: the {label} is off or no frame is available "
                f"— ask the user to turn the {label} on.]"
            )
            return
        desc = await _describe_frame(frame, label)
        noun = "shared screen" if label == "screen" else "camera"
        if desc:
            new_message.content.append(
                f"\n\n[Vision — what the user's {noun} shows right now: "
                f"{desc}]"
            )
        else:
            new_message.content.append(
                f"\n\n[Vision: unable to analyse the {noun} just now.]"
            )


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    hermes_url = os.environ.get("HERMES_URL", "http://localhost:8642")
    hermes_key = os.environ.get("HERMES_API_KEY", "no-key")
    n8n_mcp_url = os.environ.get("N8N_MCP_SERVER_URL", "")

    mcp_servers = []
    if n8n_mcp_url:
        mcp_servers.append(mcp.MCPServerHTTP(url=n8n_mcp_url))

    # STT: Deepgram PRIMARY, Google Cloud FALLBACK
    stt_chain = [
        ("Deepgram", lambda: deepgram.STT()),
        ("Google Cloud", lambda: google.STT()),
    ]
    stt = None
    for provider_name, provider_fn in stt_chain:
        try:
            stt = provider_fn()
            logger.info(f"✓ STT: Using {provider_name}")
            break
        except Exception as e:
            logger.warning(f"⚠ {provider_name} STT init failed: {e}")
    if stt is None:
        raise RuntimeError("No STT provider available")

    # TTS: Deepgram Aura PRIMARY (uses the SAME DEEPGRAM_API_KEY as STT),
    # Google Cloud FALLBACK (requires GOOGLE_APPLICATION_CREDENTIALS_JSON
    # on the Railway service — currently NOT set, so Google fails at the
    # first synthesize call). Aura's voice quality is on par with Google
    # for the British-Jarvis tone and avoids the auth gap that has been
    # silencing Jarvis to date.
    tts_chain = [
        ("Deepgram Aura", lambda: deepgram.TTS()),
        ("Google Cloud", lambda: google.TTS()),
    ]
    tts = None
    for provider_name, provider_fn in tts_chain:
        try:
            tts = provider_fn()
            logger.info(f"✓ TTS: Using {provider_name}")
            break
        except Exception as e:
            logger.warning(f"⚠ {provider_name} TTS init failed: {e}")
    if tts is None:
        raise RuntimeError("No TTS provider available")

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=openai.LLM(
            model="hermes-agent",
            base_url=f"{hermes_url}/v1",
            api_key=hermes_key,
            extra_headers={"X-Hermes-Session-Id": ctx.room.name},
        ),
        tts=tts,
        mcp_servers=mcp_servers,
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(ctx.room),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
            # Required for the worker to receive the user's camera and
            # screen-share tracks (default is off → no video reaches us).
            video_enabled=True,
        ),
    )

    # Bridge the JS UI's `lk.agent.request` text-stream topic into the
    # agent. The agent-starter-react chat box publishes typed messages
    # there; livekit-agents only auto-handles `lk.chat`, so we wire the
    # extra topic ourselves and feed it through session.generate_reply()
    # — the same entry point voice uses. Without this the worker logs
    # "ignoring text stream with topic 'lk.agent.request'" on every type.
    def _on_agent_request(reader, participant_identity: str) -> None:
        async def _process() -> None:
            try:
                text = (await reader.read_all() or "").strip()
                if text:
                    logger.info(
                        "lk.agent.request from %s: %r",
                        participant_identity, text[:200],
                    )
                    session.generate_reply(user_input=text)
            except Exception:
                logger.exception("lk.agent.request handler failed")
        asyncio.create_task(_process())

    try:
        ctx.room.register_text_stream_handler(
            "lk.agent.request", _on_agent_request
        )
    except ValueError:
        # Already registered (e.g. on a hot reload) — fine.
        pass


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )