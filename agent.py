import os
import re
import time
import base64
import asyncio
import logging
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentSession, Agent, RoomInputOptions
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


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        # Latest frame + capture time per source.
        self._cam_frame: rtc.VideoFrame | None = None
        self._cam_at: float = 0.0
        self._screen_frame: rtc.VideoFrame | None = None
        self._screen_at: float = 0.0
        self._video_tasks: set[asyncio.Task] = set()
        self._wire_video(room)

    # ── Video capture (camera + screen-share) ────────────────────────
    def _wire_video(self, room: rtc.Room) -> None:
        """Keep the latest frame from the user's camera and screen tracks."""

        async def _consume(track: rtc.VideoTrack, source) -> None:  # noqa: ANN001
            stream = rtc.VideoStream(track)
            try:
                async for ev in stream:
                    if source == rtc.TrackSource.SOURCE_SCREENSHARE:
                        self._screen_frame = ev.frame
                        self._screen_at = time.time()
                    else:
                        self._cam_frame = ev.frame
                        self._cam_at = time.time()
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

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """On a vision phrase, inject a frame description into the turn."""
        text = getattr(new_message, "text_content", "") or ""
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
            noise_cancellation=noise_cancellation.BVC()
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