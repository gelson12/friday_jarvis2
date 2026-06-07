import os
import re
import time
import json
import uuid
import base64
import asyncio
import logging
import httpx
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
import search_tools

# Accommodation booking. Lazy-import so a missing module never crashes
# worker startup. See `brain/Accommodation Booking — Implementation Plan` in
# the user's Obsidian vault for the full design.
try:
    from accommodation import (  # noqa: F401
        AccommodationService,
        Property as _AccommodationProperty,
        SearchQuery as _AccommodationSearchQuery,
        BookingRequest as _AccommodationBookingRequest,
    )
    from accommodation import nlu as _accommodation_nlu
    _ACCOMMODATION_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    AccommodationService = None  # type: ignore[assignment]
    _AccommodationProperty = None  # type: ignore[assignment]
    _AccommodationSearchQuery = None  # type: ignore[assignment]
    _AccommodationBookingRequest = None  # type: ignore[assignment]
    _accommodation_nlu = None  # type: ignore[assignment]
    _ACCOMMODATION_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "accommodation module unavailable (%s) — hotel booking disabled", _exc,
    )

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
_WAKE_RE = re.compile(
    r"\b("
    r"hey\s+|ok\s+|okay\s+|hi\s+|hello\s+|yo\s+|"
    r"good\s+(?:morning|afternoon|evening|night)\s+"
    r"|wake\s+up,?\s+|wake\s+|are\s+you\s+there,?\s+"
    r")?friday\b",
    re.I,
)
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


# ── Camera enable/disable (voice intent) ─────────────────────────────
# Distinct concern from the vision routing above: this controls whether
# the user's camera TRACK is on or off. Structured command published on
# the `ui-command` topic; the frontend hook calls
# `localParticipant.setCameraEnabled(...)`. Mirrors OpenJarvis.
UI_COMMAND_TOPIC = "ui-command"

_CAM_WORD = re.compile(r"\b(camera|cam|webcam|video)\b", re.I)
_CAM_OFF = re.compile(
    r"\b(off|disable|stop|close|kill|hide|turn it off|shut)\b", re.I
)
_CAM_ON = re.compile(
    r"\b(on|enable|start|open|show|turn it on|activate)\b", re.I
)


def _camera_intent(text: str):
    """Return True (turn on), False (turn off), or None (not a camera cmd).

    'off' wins if both polarities somehow appear in the same utterance
    ("turn the camera off, not on").
    """
    if not text or not _CAM_WORD.search(text):
        return None
    if _CAM_OFF.search(text):
        return False
    if _CAM_ON.search(text):
        return True
    return None


# Matches the noun-phrase only. Conservative — requires the exact two-word
# phrase so a stray "gesture" in conversation doesn't toggle the mode.
_GESTURE_MODE_RE = re.compile(
    r"\b(gesture\s+mode|hand\s+tracking|gesture\s+control)\b", re.I
)


def _gesture_mode_intent(text: str):
    """Return True (turn on), False (turn off), or None (not a gesture-mode cmd).

    Reuses _CAM_ON / _CAM_OFF as the polarity vocabulary so the toggle
    phrasing stays uniform across modes. 'off' wins if both polarities
    appear.
    """
    if not text or not _GESTURE_MODE_RE.search(text):
        return None
    if _CAM_OFF.search(text):
        return False
    if _CAM_ON.search(text):
        return True
    return None


# ── Screen widgets (floating HUD panels) ─────────────────────────────
# The browser renders draggable, semi-transparent widget panels. The
# worker summons them by publishing a JSON command on the `jarvis-ui`
# data topic in the *user's* room (not the desktop-control room).
_UI_TOPIC = "jarvis-ui"
_UI_OPEN_RE = re.compile(
    r"\b(open|show|bring up|display|pop up|put up|launch)\b", re.I
)
_UI_CLOSE_RE = re.compile(
    r"\b(close|hide|dismiss|get rid of|take down)\b", re.I
)
# Widget kind → regex of words the user might use for it.
_WIDGET_WORDS: dict[str, str] = {
    "chat": r"chat|conversation|transcript|messages?",
    "music": r"music|spotify|player|songs?",
    "news": r"news|headlines",
    "youtube": r"youtube|videos?",
    "maps": r"maps?|directions|navigation",
    "search": r"search|google",
    "apps": r"apps?|services|programs?|launcher",
    "system": r"system|diagnostics",
    "clock": r"clock|chronometer",
}


def _widget_from_text(text: str) -> str | None:
    """Return the widget kind named in ``text``, or None."""
    for kind, pattern in _WIDGET_WORDS.items():
        if re.search(rf"\b(?:{pattern})\b", text, re.I):
            return kind
    return None


# ── Content intents (search / video / news / maps / live browser) ────
# The voice LLM does not reliably emit tool calls — it tends to just
# chat — so the web_search / search_youtube / show_news / show_map /
# open_browser tools often never fire. We instead detect these intents
# with a regex on the user's turn (the same approach the wake-word and
# widget handlers use) and call the matching method directly.
_BROWSER_RE = re.compile(
    r"\b(?:open|launch|start|bring up)\b[^.]*\bbrowser\b|\bopen chrome\b", re.I
)
_URL_RE = re.compile(
    r"\b(?:go to|browse to|navigate to|open)\s+"
    r"((?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s,]*)", re.I
)
_YOUTUBE_RE = re.compile(r"\byoutube\b|\bvideos?\s+(?:of|about|for)\b", re.I)
_NEWS_RE = re.compile(r"\b(?:news|headlines)\b", re.I)
_MAP_RE = re.compile(r"\b(?:maps?|directions?)\b", re.I)
_WEBSEARCH_RE = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|google)|google|look\s+up|"
    r"web\s+search|search\s+for)\b", re.I
)
# Accommodation booking intent. Catches "find me a hotel", "book a place",
# "Airbnb in Lisbon", etc. See `brain/Accommodation Booking — Feasibility &
# Architecture` in the user's vault.
_ACCOMMODATION_RE = re.compile(
    r"\b(hotel|airbnb|accommodation|vacation\s+rental|short\s+let|"
    r"book\s+(?:a\s+|me\s+)?(?:room|place|hotel|stay)|where\s+to\s+stay|"
    r"find\s+(?:me\s+)?(?:a\s+|an\s+)?(?:hotel|place|stay|room))\b", re.I
)
_ACCOMMODATION_BOOK_RE = re.compile(
    r"\b(?:book|reserve)\s+(?:the\s+|that\s+|it\b)", re.I
)
_ACCOMMODATION_YES_RE = re.compile(
    r"\b(?:yes|yeah|yep|sure|ok|okay|go\s+ahead|do\s+it|"
    r"book\s+it|confirm|please\s+do|sounds\s+good)\b", re.I
)
_ACCOMMODATION_NO_RE = re.compile(
    r"\b(?:no|nope|nah|cancel|never\s*mind|don'?t|stop|"
    r"actually\s+no|hold\s+on)\b", re.I
)
_ACCOMMODATION_PENDING_TTL_S = 90.0

# v0.dev website generation. Conservative — must have a build-verb AND
# a site-noun in the same utterance so "search for site builders" or
# "build me a sandwich" don't trigger.
_SITE_BUILD_RE = re.compile(
    r"\b(?:build|design|create|make|generate|spin\s+up|set\s+up|whip\s+up)\b"
    r"[^.]*?"
    r"\b(?:website|web\s*site|site|landing\s+page|webpage|web\s*page|app)\b",
    re.I,
)
# Pulls preview URL out of the v0 assistant message.
_V0_PREVIEW_URL_RE = re.compile(
    r"https?://(?:v0\.dev/chat/|[a-z0-9-]+\.vusercontent\.net/?|"
    r"[a-z0-9-]+\.vercel\.app/?)\S*",
    re.IGNORECASE,
)
V0_API_BASE = os.environ.get("V0_API_BASE", "https://api.v0.dev/v1")
V0_MODEL = os.environ.get("V0_MODEL", "v0-1.5-md")

# An explicit "put it on screen" verb. The bare nouns `news`/`maps` match
# far too eagerly ("any news on my project" should be answered, not turned
# into a news panel), so those two intents additionally require this verb.
_CONTENT_VERB = re.compile(
    r"\b(show|open|bring up|pull up|display|get me|give me|pop up|put up|"
    r"launch|see)\b", re.I
)


# Lead / filler / command words stripped to recover the bare query from a
# spoken request like "can you play a video of cars on youtube".
_QUERY_NOISE = re.compile(
    r"\b(?:hey |ok |okay )?friday\b"
    r"|\b(?:can|could|would|will)\s+you\b|\bplease\b|\bfor me\b"
    # Polite request stems. The previous version only caught "I want to"
    # / "I'd like to" — it dropped "I would like to google Tom Cruise"
    # into the LLM as junk ("I would like to Tom Cruise"). Now covers
    # like/love/want/prefer in both contracted ("I'd") and uncontracted
    # ("I would") form, plus "let's" / "let me".
    r"|\bi\s+(?:want|need|wanna)(?:\s+to|\s+you\s+to)?\b"
    r"|\bi\s+would\s+(?:like|love|want|prefer)(?:\s+to|\s+you\s+to)?\b"
    r"|\bi'?d\s+(?:like|love|want|prefer)(?:\s+to|\s+you\s+to)?\b"
    r"|\blet'?s\b|\blet\s+me\b"
    r"|\b(?:search(?:\s+the\s+(?:web|internet))?(?:\s+for)?|google|look\s+up"
    r"|web\s+search(?:\s+for)?|find(?:\s+me)?|show(?:\s+me)?|bring\s+up"
    r"|pull\s+up|put\s+up|open|play|get\s+me|display|watch|see)\b"
    r"|\bon\s+(?:the\s+)?(?:web|internet)\b|\bonline\b",
    re.I,
)


def _clean_query(text: str) -> str:
    """Strip command / filler words to recover the bare search query."""
    q = " " + (text or "") + " "
    prev = None
    while prev != q:
        prev = q
        q = _QUERY_NOISE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip(" ,.?!-\"'")
    # Drop a dangling leading article / filler pronoun left after the
    # command word is gone ("play a video of cars" -> "a cars" -> "cars";
    # "google me tom cruise" -> "me tom cruise" -> "tom cruise"). "me" /
    # "for me" added because "google me X" / "search me X" are common
    # spoken forms.
    q = re.sub(r"^(?:a|an|the|some|my|me|for\s+me)\b\s*", "", q, flags=re.I)
    return q


def _content_intent(text: str):
    """Detect a HUD content intent. Returns ``(kind, arg)`` or ``None``.

    kind ∈ {browser, youtube, news, maps, web}; ``arg`` is the query/URL
    ('' is allowed for browser and news, where it is optional).
    """
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()

    url = _URL_RE.search(t)
    if _BROWSER_RE.search(low):
        return ("browser", url.group(1) if url else "")
    if url:
        return ("browser", url.group(1))

    # "google map(s)" is unambiguous — must route to maps, NOT websearch.
    # Without this, the bare "google" in _WEBSEARCH_RE swallows the phrase
    # and we search the web for "map" (zero useful results).
    if re.search(r"\bgoogle\s+maps?\b", low):
        q = re.sub(r"\b(?:google\s+)?maps?\b|\bnavigation\b", " ", t, flags=re.I)
        q = _clean_query(q)
        q = re.sub(r"^(?:of|to|for|the|on)\s+", "", q, flags=re.I).strip()
        return ("maps", q)

    if _YOUTUBE_RE.search(low):
        # The query can sit either side of "youtube" ("cars on youtube",
        # "youtube for cars"), so strip every youtube/video marker and the
        # command words wrapping it — what is left is the query itself.
        q = re.sub(r"\b(?:on|from|in|via|over\s+on)\s+youtube\b", " ", t, flags=re.I)
        q = re.sub(r"\byoutube\b", " ", q, flags=re.I)
        q = re.sub(r"\b(?:videos?|clips?|footage)\s+(?:of|about|for|on|with)\b",
                   " ", q, flags=re.I)
        q = re.sub(r"\b(?:videos?|clips?|footage)\b", " ", q, flags=re.I)
        return ("youtube", _clean_query(q))

    if _NEWS_RE.search(low) and (
        _CONTENT_VERB.search(low)
        or "headlines" in low
        # Bare-noun triggers so "what's the news", "any news",
        # "tell me the news", "catch me up", "latest news" all fire.
        or re.search(
            r"\b(?:what(?:'?s)?|what\s+is|any|tell\s+me|"
            r"catch\s+me\s+up|update\s+me|latest|breaking)\b",
            low,
        )
    ):
        q = re.sub(r"\b(?:news|headlines)\b|\b(?:about|on|regarding)\b",
                   " ", t, flags=re.I)
        q = re.sub(
            r"\b(?:what(?:'?s)?|what\s+is|any|tell\s+me|"
            r"catch\s+me\s+up|update\s+me|latest|breaking)\b",
            " ", q, flags=re.I,
        )
        return ("news", _clean_query(q))

    if _MAP_RE.search(low) and (
        _CONTENT_VERB.search(low)
        or re.search(r"\b(?:directions?\s+to|map\s+of|navigate\s+to|where\s+is)\b",
                     low)
    ):
        q = re.sub(r"\b(?:google\s+)?maps?\b|\bnavigation\b", " ", t, flags=re.I)
        q = _clean_query(q)
        q = re.sub(r"^(?:of|to|for|the)\s+", "", q, flags=re.I).strip()
        return ("maps", q)

    # v0.dev website generation — BEFORE websearch so "create a site
    # for cats" doesn't get caught as a web search.
    if _SITE_BUILD_RE.search(low):
        q = re.sub(
            r"\b(?:build|design|create|make|generate|spin\s+up|set\s+up|whip\s+up)\b",
            " ", t, flags=re.I,
        )
        q = re.sub(
            r"\b(?:website|web\s*site|site|landing\s+page|webpage|web\s*page|app)\b",
            " ", q, flags=re.I,
        )
        q = re.sub(r"\b(?:me|a|an|the|that|which|for|about)\b", " ",
                   q, flags=re.I)
        return ("v0", _clean_query(q))

    if _WEBSEARCH_RE.search(low):
        return ("web", _clean_query(t))

    return None


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
    """Lazy LiveKit connection to the desktop-bridge control room.

    Also a presence index: the bridge processes join the same room with
    identity ``desktop-bridge-<machine>``, so the worker can know exactly
    which PCs are reachable without polling.
    """

    def __init__(self) -> None:
        self._room: rtc.Room | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _machine_from_identity(identity: str) -> str | None:
        if not identity or not identity.startswith("desktop-bridge-"):
            return None
        return identity[len("desktop-bridge-"):].strip().lower() or None

    def online_machines(self) -> list[str]:
        """Snapshot of machines whose desktop-bridge is in the room."""
        if self._room is None:
            return []
        machines: set[str] = set()
        for p in self._room.remote_participants.values():
            m = self._machine_from_identity(getattr(p, "identity", "") or "")
            if m:
                machines.add(m)
        return sorted(machines)

    def is_online(self, machine: str) -> bool:
        m = (machine or "").strip().lower()
        if m in ("", "all", "any"):
            return bool(self.online_machines())
        return m in set(self.online_machines())

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


# ── Mobile bridge (operate the user's Android phone) ─────────────────
# A `mobile-bridge` APK runs on the user's phone, joins the SAME
# `jarvis-control` LiveKit room outbound, identity `mobile-bridge-
# <phone>`, listens on `mobile-cmd`, publishes `mobile-result`. Separate
# topics from desktop-cmd keep the wire contracts cleanly versioned.
_MOBILE_TOPIC_CMD = "mobile-cmd"
_MOBILE_TOPIC_RESULT = "mobile-result"
_MOBILE_IDENTITY_PREFIX = "mobile-bridge-"


class MobileBridge:
    """Lazy LiveKit connection to the mobile-bridge control room.

    Mirrors DesktopBridge but for the phone APK. Same control room,
    different topics + identity prefix.
    """

    def __init__(self) -> None:
        self._room: rtc.Room | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _phone_from_identity(identity: str) -> str | None:
        if not identity or not identity.startswith(_MOBILE_IDENTITY_PREFIX):
            return None
        return identity[len(_MOBILE_IDENTITY_PREFIX):].strip().lower() or None

    def online_phones(self) -> list[str]:
        if self._room is None:
            return []
        phones: set[str] = set()
        for p in self._room.remote_participants.values():
            name = self._phone_from_identity(getattr(p, "identity", "") or "")
            if name:
                phones.add(name)
        return sorted(phones)

    def is_online(self, phone: str) -> bool:
        p = (phone or "").strip().lower()
        if p in ("", "any", "all", "phone", "mobile"):
            return bool(self.online_phones())
        return p in set(self.online_phones())

    async def _ensure(self) -> rtc.Room | None:
        async with self._lock:
            if self._room is not None:
                return self._room
            url = os.environ.get("LIVEKIT_URL", "")
            key = os.environ.get("LIVEKIT_API_KEY", "")
            secret = os.environ.get("LIVEKIT_API_SECRET", "")
            if not (url and key and secret):
                logger.warning("mobile bridge: LIVEKIT_* env not set")
                return None
            token = (
                api.AccessToken(key, secret)
                .with_identity(f"friday-worker-mob-{uuid.uuid4().hex[:8]}")
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
                if packet.topic != _MOBILE_TOPIC_RESULT:
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
            logger.info("mobile bridge: connected to '%s'", _CONTROL_ROOM)
            return room

    async def send(
        self, target: str, cmd: str, args: dict, timeout: float = 30.0
    ) -> dict:
        room = await self._ensure()
        if room is None:
            return {"error": "mobile bridge unavailable (LIVEKIT_* unset)"}
        cmd_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = fut
        payload = json.dumps(
            {"id": cmd_id, "target": target, "cmd": cmd, "args": args}
        ).encode("utf-8")
        try:
            await room.local_participant.publish_data(
                payload, reliable=True, topic=_MOBILE_TOPIC_CMD
            )
            msg = await asyncio.wait_for(fut, timeout)
            return msg.get("result", {})
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            return {
                "error": f"no response from the '{target}' phone — is the "
                f"Jarvis Mobile Bridge app open and connected?"
            }
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(cmd_id, None)
            return {"error": str(exc)}


# ── Mobile intent routing ────────────────────────────────────────────
# Broad gate: utterance MIGHT be a mobile request. Generous net.
_MOBILE_HINT_RE = re.compile(
    r"\b("
    r"phone|mobile|cell|cellphone|smartphone|android|pixel|oneplus|samsung|"
    r"text|texts|sms|"
    r"call|dial|ring|"
    r"contact|contacts|phonebook|phone\s+book|"
    r"whatsapp|wapp|whats\s+app|"
    r"battery|charge|charging|signal|reception|"
    r"instagram|insta|tiktok|tik\s+tok|facebook|messenger|youtube|"
    r"install|uninstall|"
    r"app|apps"
    r")\b",
    re.I,
)

# Explicit machine-clause variant ("on my phone").
_MOBILE_MACHINE_RE = re.compile(
    r"\b(?:on|via|through|using|in|to|from)\s+"
    r"(?:my\s+|the\s+|this\s+)?"
    r"(phone|mobile|cell|cellphone|smartphone|android|pixel|oneplus|samsung)\b"
    r"|\bmy\s+(phone|mobile|cell|pixel|oneplus|samsung)\b",
    re.I,
)

_BRIDGE_MOBILE_COMMANDS_GUIDE = """\
Available mobile-bridge commands (the user's Android phone):

  sms_list         {"limit": 10, "number_filter": "<optional>"}
  sms_send         {"number": "<phone>", "message": "<text>"}
  contacts_search  {"query": "<name>", "limit": 10}
  dial             {"number": "<phone>"}                       # opens dialer
  open_app         {"name": "<app name>" or "package": "<bundle id>"}
  list_apps        {}                                          # for fuzzy resolution
  install_app      {"package": "<bundle id>"}                  # opens Play Store
  uninstall_app    {"package": "<bundle id>"}                  # opens uninstall dialog
  open_url         {"url": "<full URL>"}                       # opens browser
  whatsapp_send    {"number": "<phone>", "message": "<text>"}  # opens WhatsApp pre-filled
  device_status    {}                                          # battery / model / signal
  host_info        {}

NOT supported (no public personal-account API exists): sending DMs on
Instagram, Facebook Messenger, or TikTok; posting to those platforms.
For those intents pick `open_app` with the app name OR `open_url` with
a deep-link such as instagram.com/<user>, m.me/<user>, ig.me/<user>."""


_MOBILE_ROUTER_MODEL = os.environ.get(
    "OPENJARVIS_ROUTER_MODEL", "google/gemini-2.0-flash-001"
)


async def _route_mobile(text: str, phones_online: list[str]) -> dict | None:
    """Turn a free-form spoken request into a structured mobile-bridge command.

    Returns ``{"phone", "cmd", "args", "say", "contact_query"?}`` on
    success, or None when the request isn't a mobile command / the LLM
    is unavailable.
    """
    client = _get_router_client()
    if client is None or not text:
        return None
    online = ", ".join(phones_online) if phones_online else "none right now"
    system = (
        "You translate ONE spoken request into ONE command for the user's "
        "Android phone. Output strict JSON only — no prose, no code fences.\n\n"
        "Schema. Either:\n"
        '  {"mobile": false}   — when the request is NOT about operating '
        "their phone.\n"
        "Or:\n"
        '  {"mobile": true, "phone": "<phone_name>"|"any", '
        '"command": "<name>", "args": {...}, '
        '"contact_query": "<name>"?, '
        '"say": "<one short butler-voice sentence>"}\n\n'
        f"{_BRIDGE_MOBILE_COMMANDS_GUIDE}\n\n"
        "Rules:\n"
        "- Default phone: 'any'. Phones online right now: " + online + ".\n"
        "- If the user names a CONTACT instead of a phone number for sms_send"
        " / dial / whatsapp_send, set `contact_query` to the name and leave "
        '`args.number` empty — the worker will resolve it via contacts_search.\n'
        "- For 'open <social app>' use open_app with the app name.\n"
        "- For 'send Instagram DM / FB message / TikTok message': output\n"
        '  {"mobile": true, "phone": "any", "command": "open_app", '
        '"args": {"name": "<instagram|messenger|tiktok>"}, '
        '"say": "I can\'t message <Platform> directly from here, sir — '
        'opening the app for you."}\n'
        "- `dial` opens the dialer (no auto-call); `whatsapp_send` opens "
        "WhatsApp pre-filled (user taps send).\n"
        "- `say` is one short butler sentence ('Texting Mum, sir.', "
        "'On it, sir.').\n"
        "- If conversation / question / unclear, output {\"mobile\": false}."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_MOBILE_ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mobile router LLM failed: %s", exc)
        return None
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mobile router non-JSON: %s — %r", exc, raw[:200])
        return None
    if not isinstance(data, dict) or not data.get("mobile"):
        return None
    cmd = (data.get("command") or "").strip()
    if not cmd:
        return None
    return {
        "phone": str(data.get("phone") or "any").strip().lower(),
        "cmd": cmd,
        "args": data.get("args") or {},
        "contact_query": (data.get("contact_query") or "").strip(),
        "say": (data.get("say") or "").strip(),
    }


def _mobile_reply(phone: str, cmd: str, args: dict, res: dict) -> str:
    """Turn a mobile-bridge result dict into a butler-tone confirmation."""
    if res.get("error"):
        return f"That didn't work, sir — {res['error']}"
    if cmd == "sms_send":
        return "Texted them, sir."
    if cmd == "sms_list":
        messages = res.get("messages") or []
        if not messages:
            return "No recent messages, sir."
        # Speak top 3 succinctly.
        lines = []
        for m in messages[:3]:
            sender = m.get("from", "Unknown")
            body = (m.get("body", "") or "").strip().replace("\n", " ")
            if len(body) > 120:
                body = body[:120] + "…"
            lines.append(f"{sender}: {body}")
        more = "" if len(messages) <= 3 else f" Plus {len(messages) - 3} more."
        return "Your latest texts, sir. " + " ... ".join(lines) + more
    if cmd == "contacts_search":
        contacts = res.get("contacts") or []
        if not contacts:
            return "No contacts matched, sir."
        if len(contacts) == 1:
            c = contacts[0]
            return f"That's {c.get('name', 'them')} at {c.get('number', '')}, sir."
        names = ", ".join(c.get("name", "?") for c in contacts[:3])
        return f"Found {len(contacts)} contacts, sir: {names}."
    if cmd == "dial":
        return "Dialler's up, sir — tap to call."
    if cmd == "whatsapp_send":
        return "WhatsApp's open and ready, sir — tap send."
    if cmd == "open_app":
        opened = res.get("opened") or args.get("name") or args.get("package")
        return f"Opening {opened}, sir."
    if cmd == "open_url":
        return "On screen, sir."
    if cmd == "list_apps":
        apps = res.get("apps") or []
        return f"You have {len(apps)} apps installed, sir."
    if cmd == "install_app":
        return "Play Store's up, sir — tap install."
    if cmd == "uninstall_app":
        return "Uninstall prompt's up, sir."
    if cmd == "device_status":
        bat = res.get("battery_percent")
        charging = res.get("charging")
        if bat is not None:
            tail = " and charging" if charging else " and not charging"
            return f"Battery is at {bat}%{tail}, sir."
        return "Phone reports back, sir."
    if cmd == "host_info":
        model = res.get("model", "")
        return f"That's your {model or phone}, sir." if model else f"Phone {phone} online, sir."
    return f"Done on your {phone}, sir."


# ── APK build (rebuild mobile-bridge APK via VS-Code-inspiring-cat) ─
# Voice command kicks the same /tasks shell pipeline I (Claude) used
# manually to produce the first APK. Build runs ~10-15 min on the
# inspiring-cat container; APK lands as a GitHub release asset on
# the friday_jarvis2 repo; worker polls + speaks the URL when ready.
_APK_BUILD_RE = re.compile(
    r"\b("
    r"(?:build|rebuild|compile|make|create)\s+"
    r"(?:me\s+|us\s+)?(?:the\s+|a\s+|an\s+)?"
    r"(?:mobile[\s-]?bridge|android(?:\s+app)?|apk|phone\s+app)"
    r")\b",
    re.I,
)

# Dedicated mobile-bridge phrasings shortcut around the "which repo?"
# follow-up — they always mean the fixed mobile-bridge module inside
# gelson12/friday_jarvis2.
_APK_MOBILE_BRIDGE_RE = re.compile(
    r"\bmobile[\s-]?bridge\b|\bphone\s+app\b", re.I,
)

# "from <owner>/<repo>" or "from github.com/<owner>/<repo>" — captures the
# repo when the user names one. GitHub repo names allow [A-Za-z0-9._-];
# the leading owner is conservative (no dots, no leading dash) to match
# GitHub's username rules.
_APK_REPO_RE = re.compile(
    r"\b(?:from|on|out\s+of|using)\s+"
    r"(?:https?://)?(?:github\.com/)?"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)"
    r"\s*[/ ]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
    re.I,
)

# Parsed verbatim from a follow-up answer to "which repo, sir?". Looser
# than the leading-"from" version above so the user can just say
# "gelson12 slash weather-app" or "github.com/foo/bar".
_APK_REPO_BARE_RE = re.compile(
    r"(?:https?://)?(?:github\.com/)?"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)"
    r"\s*(?:/|\bslash\b|\s)\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
    re.I,
)

INSPIRING_CAT_URL = os.environ.get(
    "INSPIRING_CAT_URL",
    "https://inspiring-cat-production.up.railway.app",
).rstrip("/")
MOBILE_BRIDGE_REPO = os.environ.get(
    "MOBILE_BRIDGE_REPO", "gelson12/friday_jarvis2",
)
MOBILE_BRIDGE_BUILD_SCRIPT_URL = os.environ.get(
    "MOBILE_BRIDGE_BUILD_SCRIPT_URL",
    f"https://raw.githubusercontent.com/{MOBILE_BRIDGE_REPO}"
    "/main/scripts/build-mobile-bridge-apk.sh",
)
# Where ALL voice-built APKs are released (per user preference): single
# repo we control, single cleanup workflow, single PAT to manage. The
# source repo can be anything; releases always land here.
APK_RELEASE_REPO = os.environ.get(
    "APK_RELEASE_REPO", MOBILE_BRIDGE_REPO,
)


# ── Telegram (worker-side via Bot API; no phone required) ────────────
_TELEGRAM_RE = re.compile(
    r"\b(?:telegram|tg)\b.*?\b(?:to|send|message|tell|ping)\b"
    r"|\b(?:send|tell|message|ping)\b.*?\b(?:telegram|tg)\b",
    re.I,
)


async def _extract_telegram_payload(text: str) -> dict | None:
    """LLM-extract {contact, message} from a Telegram-send utterance."""
    client = _get_router_client()
    if client is None or not text:
        return None
    system = (
        "Extract the recipient name and the message body from the user's "
        "request to send a Telegram message. Output strict JSON only:\n"
        '  {"contact": "<name>", "message": "<body>"}\n'
        "Lowercase the contact name. Strip the verbs ('send', 'tell',\n"
        "'telegram', 'message', etc.) from the message body."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_MOBILE_ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
        data = json.loads((resp.choices[0].message.content or "").strip())
        if not data.get("contact") or not data.get("message"):
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram extractor failed: %s", exc)
        return None


# ── Desktop control intent (regex fallback) ──────────────────────────
# The desktop-control @function_tools (open_on_machine, …) only fire if
# the voice LLM emits a tool call, which it does not do reliably. This
# regex fallback detects an explicit desktop request on the user's turn
# and drives the DesktopBridge directly. An explicit "on my laptop / on
# the rog" clause is required so ordinary conversation never reaches the
# user's machines by accident.
# The machine clause. Accept any natural preposition ("on/in/of/from my
# laptop", "of my pc") AND a bare "my laptop / my pc" — Deepgram and
# ordinary speech vary the wording, and the old `on`-only form silently
# dropped "list the files in my laptop" / "lower the volume of my pc".
_DESKTOP_MACHINE_RE = re.compile(
    r"\b(?:on|in|of|from|to|with|using|inside|across)\s+"
    r"(?:my\s+|the\s+|this\s+|our\s+)?"
    r"(laptop|rog|pc|desktop|computer|machine)\b"
    r"|\bmy\s+(laptop|rog|pc|computer|machine)\b",
    re.I,
)
_DESKTOP_OPEN_RE = re.compile(r"\b(open|launch|start)\b", re.I)

# Volume control words. `_VOL_DOWN`/`_VOL_UP` also cover the bare verbs
# ("make it quieter", "turn it up") so a direction is always resolvable.
_VOLUME_WORD = re.compile(
    r"\b(volume|sound|audio|mute|unmute|louder|quieter)\b", re.I
)
_VOL_DOWN = re.compile(
    r"\b(down|decrease|lower|reduce|quiet\w*|soft\w*|less)\b", re.I
)
_VOL_UP = re.compile(
    r"\b(up|increase|raise|boost|crank|loud\w*|higher|more)\b", re.I
)
_VOL_SET = re.compile(r"\b(?:to|at)\s+(\d{1,3})\b", re.I)

# Standalone volume intent — fires WITHOUT requiring an "on my laptop"
# clause, so "mute" / "lower the volume" go through the disambiguation
# router instead of falling into the content matcher. Conservative on
# purpose: the bare word "volume" alone must not trigger (e.g. "what's
# the volume of a sphere"); requires a verb or louder/quieter/mute.
_VOLUME_INTENT_RE = re.compile(
    r"\b("
    r"mute|unmute|"
    r"(?:turn|crank|bump|set|put)\s+(?:the\s+|down\s+|up\s+)?"
    r"(?:volume|sound|audio|it|that|music)|"
    r"(?:lower|raise|increase|decrease|reduce|boost|drop|bring\s+down|"
    r"bring\s+up)\s+(?:the\s+)?(?:volume|sound|audio|it|that|music)|"
    r"volume\s+(?:up|down|to|at)|"
    r"(?:louder|quieter|softer)"
    r")\b",
    re.I,
)

# ── Clarification primitive ──────────────────────────────────────────
# Generic "I asked a question, the next user turn is the answer" state.
# Used today by volume disambiguation; reused later by camera selection,
# app_open targeting, etc. Single-slot — a new clarification REPLACES any
# previous one so the state machine never gets stuck. 30s expiry.

# Cancel/abort phrases — match before option resolution.
_CANCEL_RE = re.compile(
    r"\b(never\s*mind|nevermind|cancel|forget\s+it|skip\s+it|"
    r"nothing|drop\s+it|stop)\b",
    re.I,
)

# Ordinal word/digit forms ("the first", "second one", "3rd", "two")
# used to pick options 0/1/2 in a posed multi-choice clarification.
_ORDINAL_RE: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(?:the\s+)?(?:first|1st|one|number\s*one)\b", re.I), 0),
    (re.compile(r"\b(?:the\s+)?(?:second|2nd|two|number\s*two)\b", re.I), 1),
    (re.compile(r"\b(?:the\s+)?(?:third|3rd|three|number\s*three)\b", re.I), 2),
]

# Bulk-select phrases — applies the action to ALL options at once.
_BOTH_RE = re.compile(r"\b(both|all|everything|every\s*one)\b", re.I)


@dataclass
class PendingClarification:
    """One pending disambiguation question awaiting the user's answer.

    Stored as a single slot on `Assistant`. The resumer dict on the
    assistant maps `intent_kind` to the coroutine that finishes the
    deferred action once an option is picked.
    """
    intent_kind: str
    options: list[dict]
    original_args: dict
    prompt: str
    created_at: float
    expires_at: float


def _derive_match_words(process_name: str) -> list[str]:
    """Turn a process name into the words a user might say for it.

    e.g. 'Spotify.exe' → ['spotify']; 'chrome.exe' → ['chrome', 'browser'];
    'vlc.exe' → ['vlc', 'media player']; 'firefox.exe' → ['firefox', 'browser'].
    """
    name = (process_name or "").strip().lower()
    base = re.sub(r"\.(exe|app|bin)$", "", name).strip()
    if not base:
        return []
    out = {base}
    if base in ("chrome", "msedge", "firefox", "opera", "brave"):
        out.add("browser")
    if base in ("vlc", "wmplayer", "mpc-hc", "mpv"):
        out.add("media player")
    if base in ("wmplayer",):
        out.add("windows media player")
    if base == "msedge":
        out.add("edge")
    if base == "spotify":
        out.add("music")
    return sorted(out)


# Broad gate for "this turn MIGHT be a desktop request" — only authorises
# the LLM router call. Cheap to be loose; the router rejects false
# positives with {"desktop": false}.
_DESKTOP_HINT_RE = re.compile(
    r"\b("
    r"laptop|rog|pc|computer|machine|workstation|"
    r"file|files|folder|folders|directory|document|documents|"
    r"download|downloads|"
    r"open|launch|start|run|execute|close|kill|terminate|quit|exit|"
    r"delete|remove|trash|move|copy|rename|create|make|new|"
    r"play|pause|skip|song|songs|music|video|videos|track|tune|"
    r"volume|sound|audio|mute|unmute|louder|quieter|"
    r"memory|ram|cpu|disk|storage|space|process|processes|task|tasks|"
    r"recycle|bin|clean|cleanup|clear|"
    r"app|apps|application|applications|program|programs|"
    r"notepad|chrome|firefox|edge|spotify|word|excel|outlook|"
    r"lock|shutdown|restart|reboot|sleep|"
    r"desktop|screen|wallpaper|"
    r"diagnose|debug|status|scan|"
    r"read|aloud|contents?|txt|pdf|docx?|xlsx?|csv|"
    r"share|attach|attachment"
    r")\b",
    re.I,
)

# The desktop bridge's command catalogue, in the shape the bridge accepts.
_BRIDGE_COMMANDS_GUIDE = """\
Available bridge commands (use `command` and `args`):

  open              {"target": "<app | file/folder path | url>"}
  list_dir          {"path": "<dir>"}              # ~ = home; env vars ok
  read_file         {"path": "<file>"}
  write_file        {"path": "<file>", "content": "<text>"}
  make_dir          {"path": "<dir>"}
  delete            {"path": "<file or dir>"}      # ALWAYS Recycle Bin
  empty_recycle_bin {}
  move              {"src": "<from>", "dst": "<to>"}
  copy              {"src": "<from>", "dst": "<to>"}
  search_files      {"path": "<root>", "pattern": "<name substring>",
                     "limit": 50}
  system_status     {}                              # CPU / RAM / disk
  list_processes    {"top": 10, "by": "memory" | "cpu"}
  close_app         {"name": "<process name, e.g. chrome>"}
  volume            {"action": "up"|"down"|"mute"|"unmute"|"set",
                     "level": 0-100 (for 'set')}
  audio_sessions    {}                              # list per-app audio sessions
  app_volume        {"process_name": "<name>",
                     "action": "up"|"down"|"mute"|"unmute"|"set",
                     "level": 0-100 (for 'set'),
                     "step": 0.0-1.0 (for up/down)}
  media_key         {"key": "play_pause"|"next"|"previous"|"stop"}
  play_media        {"query": "<song or video name>"}
  lock_workstation  {}
  shell             {"command": "<PowerShell, last-resort escape hatch>"}

Standard folders: ~\\Downloads, ~\\Documents, ~\\Desktop, ~\\Pictures,
~\\Music, ~\\Videos."""


_ROUTER_MODEL = os.environ.get(
    "OPENJARVIS_ROUTER_MODEL", "google/gemini-2.0-flash-001"
)
_router_client: AsyncOpenAI | None = None


def _get_router_client() -> AsyncOpenAI | None:
    """OpenRouter client for the desktop intent router."""
    global _router_client
    if _router_client is not None:
        return _router_client
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        logger.warning(
            "desktop router unavailable: OPENROUTER_API_KEY not set"
        )
        return None
    _router_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key
    )
    return _router_client


async def _route_desktop(text: str, machines_online: list[str]) -> dict | None:
    """Turn a free-form spoken request into a structured bridge command.

    Returns ``{"machine", "cmd", "args", "say"}`` on success, or None when
    the request isn't a desktop command / the LLM is unavailable.
    """
    client = _get_router_client()
    if client is None or not text:
        return None
    online = ", ".join(machines_online) if machines_online else "none right now"
    system = (
        "You translate ONE spoken request into ONE command for the user's "
        "Windows machines (laptop and rog). Output strict JSON only — no "
        "prose, no code fences.\n\n"
        "Schema. Either:\n"
        '  {"desktop": false}   — when the request is NOT about operating '
        "their computer.\n"
        "Or:\n"
        '  {"desktop": true, "machine": "laptop"|"rog"|"all", '
        '"command": "<name>", "args": {...}, '
        '"say": "<one short butler-voice sentence>"}\n\n'
        f"{_BRIDGE_COMMANDS_GUIDE}\n\n"
        "Rules:\n"
        "- Default machine: laptop. Bridges online right now: "
        + online + ".\n"
        "- Prefer a NAMED command. Use `shell` only when nothing else fits.\n"
        "- `delete` always sends to the Recycle Bin (never permanent).\n"
        "- For app names like Word/Excel/Notepad use `open` with target "
        "like 'notepad' or 'winword'. For URLs use `open` with the URL.\n"
        "- `say` is one short sentence, butler tone ('Right away, sir.', "
        "'Done, sir.', 'On it, sir.').\n"
        "- If the request is conversation, a question, or unclear, output "
        '{"desktop": false}.'
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("desktop router LLM failed: %s", exc)
        return None
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "desktop router non-JSON: %s — %r", exc, raw[:200]
        )
        return None
    if not isinstance(data, dict) or not data.get("desktop"):
        return None
    cmd = (data.get("command") or "").strip()
    if not cmd:
        return None
    return {
        "machine": _norm_machine(str(data.get("machine") or "laptop")),
        "cmd": cmd,
        "args": data.get("args") or {},
        "say": (data.get("say") or "").strip(),
    }

# Spoken folder names → a path the desktop-bridge resolves via
# os.path.expanduser/expandvars. "list my downloads folder" -> "~\Downloads".
_KNOWN_DIRS = {
    "downloads": "~\\Downloads", "download": "~\\Downloads",
    "desktop": "~\\Desktop",
    "documents": "~\\Documents", "document": "~\\Documents",
    "pictures": "~\\Pictures", "picture": "~\\Pictures",
    "videos": "~\\Videos", "video": "~\\Videos",
    "music": "~\\Music",
    "home folder": "~", "home directory": "~", "user folder": "~",
}
# An explicit path: drive (C:\...), ~/..., %VAR%..., or a UNC \\share.
_EXPLICIT_PATH_RE = re.compile(
    r"([a-zA-Z]:\\[^\s,]*|[~%][^\s,]*|\\\\[^\s,]+)"
)


def _desktop_path(text: str) -> str:
    """Best-effort file/folder path from a spoken request.

    Order: an explicit path, then a known Windows folder name, then the
    bare word before "folder"/"directory".
    """
    m = _EXPLICIT_PATH_RE.search(text)
    if m:
        return m.group(1)
    low = text.lower()
    for word, path in _KNOWN_DIRS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            return path
    m = re.search(r"\b([\w.\-]+)\s+(?:folder|directory|dir)\b", text, re.I)
    if m:
        return m.group(1)
    return ""


def _desktop_intent(text: str):
    """Detect a desktop-control intent. Returns ``(machine, cmd, args)`` or None.

    Requires an explicit "on my laptop / on the rog" clause so ordinary
    conversation never reaches the user's machines by accident.
    """
    if not text:
        return None
    m = _DESKTOP_MACHINE_RE.search(text)
    if not m:
        return None
    # The regex has two machine groups (prepositional vs. bare "my X").
    machine = _norm_machine(m.group(1) or m.group(2))
    # Drop the machine clause so it isn't mistaken for a path/target.
    body = _DESKTOP_MACHINE_RE.sub(" ", text)
    low = body.lower()
    path = _desktop_path(body)

    # Volume — checked first; "decrease the volume" has no path/open verb.
    if _VOLUME_WORD.search(low):
        if re.search(r"\bunmute\b", low):
            return (machine, "volume", {"action": "unmute"})
        if re.search(r"\bmute\b", low):
            return (machine, "volume", {"action": "mute"})
        setm = _VOL_SET.search(low)
        if setm and re.search(r"\b(volume|sound|audio)\b", low):
            return (machine, "volume",
                    {"action": "set", "level": int(setm.group(1))})
        if _VOL_DOWN.search(low):
            return (machine, "volume", {"action": "down"})
        if _VOL_UP.search(low):
            return (machine, "volume", {"action": "up"})
        return None

    if re.search(r"\b(run|execute)\b", low) and "command" in low:
        cmd = re.sub(r"^.*?\bcommand\b[:\s]*", "", body, flags=re.I)
        cmd = cmd.strip(" ,.!?-\"'")
        return (machine, "shell", {"command": cmd}) if cmd else None
    if re.search(r"\bread\b", low) and re.search(r"\bfile\b|\.\w{1,5}\b", low):
        return (machine, "read_file", {"path": path}) if path else None
    # List a folder. A "strong" word (list/files/folder/…) stands alone;
    # a "weak" one (show/see/what's) still needs a path so chit-chat like
    # "what's up on my laptop" doesn't get treated as a directory listing.
    # When no folder is named ("all the files in my laptop"), default to
    # the user's home directory.
    strong_list = re.search(
        r"\b(list|files?|folder|directory|directories|contents?|browse|dir)\b",
        low,
    )
    weak_list = re.search(r"\b(show|see|view|what'?s)\b", low)
    if strong_list or (path and weak_list):
        return (machine, "list_dir", {"path": path or "~"})
    if _DESKTOP_OPEN_RE.search(low):
        target = re.sub(r"^.*?\b(?:open|launch|start)\b\s*", "", body,
                        flags=re.I)
        target = re.sub(r"\b(?:the|a|an|please|for me|up)\b", " ", target,
                        flags=re.I)
        target = re.sub(r"\s+", " ", target).strip(" ,.!?-\"'")
        return (machine, "open", {"target": target}) if target else None
    return None


def _desktop_reply(machine: str, cmd: str, args: dict, res: dict) -> str:
    """Format a desktop-bridge result into a one-line spoken confirmation."""
    if not isinstance(res, dict) or res.get("error"):
        err = res.get("error") if isinstance(res, dict) else "no response"
        return f"I couldn't do that on the {machine}, sir — {err}"
    if cmd == "open":
        return (
            f"Opened {res.get('opened', args.get('target', ''))} "
            f"on the {machine}, sir."
        )
    if cmd == "volume":
        action = args.get("action", "")
        if action == "mute":
            return f"Muted the {machine}, sir."
        if action == "unmute":
            return f"Unmuted the {machine}, sir."
        level = res.get("level")
        if level is not None:
            return f"Volume on the {machine} is now {level} percent, sir."
        return f"Volume on the {machine} adjusted, sir."
    if cmd == "list_dir":
        entries = res.get("entries", [])
        if not entries:
            return f"That folder on the {machine} is empty, sir."
        names = [e.get("name", "") for e in entries[:8]]
        extra = f", and {len(entries) - 8} more" if len(entries) > 8 else ""
        return (
            f"The {machine} folder holds {len(entries)} items, sir — "
            f"{', '.join(names)}{extra}."
        )
    if cmd == "read_file":
        content = (res.get("content") or "").strip()
        if not content:
            return f"That file on the {machine} is empty, sir."
        return f"Here is that file on the {machine}, sir: {content[:300]}"
    if cmd == "shell":
        out = (res.get("stdout") or "").strip()
        rc = res.get("returncode")
        tail = f" {out[:280]}" if out else ""
        return f"Done on the {machine}, sir — exit {rc}.{tail}"
    if cmd == "system_status":
        cpu = res.get("cpu_percent")
        ram_used = res.get("ram_used_gb")
        ram_total = res.get("ram_total_gb")
        ram_pct = res.get("ram_percent")
        disk_free = res.get("disk_free_gb")
        return (
            f"On the {machine}, sir: CPU at {cpu} percent, "
            f"RAM at {ram_pct} percent — {ram_used} of {ram_total} gigs "
            f"used — and {disk_free} gigs free on the system drive."
        )
    if cmd == "list_processes":
        top = res.get("top") or []
        if not top:
            return f"No processes to report on the {machine}, sir."
        head = ", ".join(
            f"{p.get('name','?')} {p.get('ram_mb',0):.0f} MB"
            for p in top[:5]
        )
        return f"Top on the {machine}, sir: {head}."
    if cmd == "close_app":
        n = res.get("closed", 0)
        name = args.get("name", "that app")
        if not n:
            return f"I don't see {name} running on the {machine}, sir."
        plural = "" if n == 1 else " instances"
        return f"Closed {n}{plural} of {name} on the {machine}, sir."
    if cmd == "delete":
        to = res.get("to", "")
        if to == "recycle_bin":
            return f"Sent it to the Recycle Bin on the {machine}, sir."
        if to == "permanent":
            return f"Permanently deleted on the {machine}, sir."
        return f"Deleted on the {machine}, sir."
    if cmd == "empty_recycle_bin":
        return f"Recycle Bin emptied on the {machine}, sir."
    if cmd == "search_files":
        matches = res.get("matches") or []
        if not matches:
            return f"No matches on the {machine}, sir."
        head = [os.path.basename(p) for p in matches[:5]]
        tail = (f", and {len(matches) - 5} more"
                if len(matches) > 5 else "")
        return (
            f"Found {len(matches)} on the {machine}, sir — "
            f"{', '.join(head)}{tail}."
        )
    if cmd in ("move", "copy"):
        verb = "Moved" if cmd == "move" else "Copied"
        return f"{verb} it on the {machine}, sir."
    if cmd == "make_dir":
        return f"Folder created on the {machine}, sir."
    if cmd == "write_file":
        return f"File written on the {machine}, sir."
    if cmd == "media_key":
        key = (args.get("key") or "").lower()
        labels = {
            "play_pause": "Toggled playback", "play": "Playing",
            "pause": "Paused", "next": "Skipping to the next track",
            "previous": "Going back a track", "prev": "Going back a track",
            "stop": "Stopped",
        }
        return f"{labels.get(key, 'Done')} on the {machine}, sir."
    if cmd == "play_media":
        if res.get("playing") == "youtube":
            return (
                f"I didn't find that on the {machine}, sir — opened a "
                "YouTube search instead."
            )
        playing = res.get("playing", "")
        if playing:
            return (
                f"Playing {os.path.basename(playing)} on the {machine}, sir."
            )
        return f"Playing it on the {machine}, sir."
    if cmd == "lock_workstation":
        return f"Locked the {machine}, sir."
    if cmd == "host_info":
        host = res.get("hostname", machine)
        return f"The {machine} reports as {host}, sir — online."
    return f"Done on the {machine}, sir."


# ── OpenCTI ↔ Jarvis intelligence layer ──────────────────────────────
# Mirror of the same block in OpenJarvis_lk/livekit/worker.py. Keep the
# two in sync — they share the same router prompt, the same GraphQL
# operations, the same idle/lifecycle semantics. Voice surface is
# identical so the user gets the same Tony-Stark experience whichever
# Jarvis variant they wake.

_CTI_HINT_RE = re.compile(
    r"\b("
    r"opencti|cti|"
    r"intel|intelligence|threat|threats|"
    r"observable|observables|indicator|indicators|"
    r"incident|incidents|investigate|investigation|"
    r"adversary|actor|ioc|iocs|stix|"
    r"kill\s?chain|campaign|malware|phishing|breach|"
    r"suspicious|"
    r"log\s+(?:the\s+|that\s+|this\s+)?(?:domain|ip|url|hash|email|file)|"
    r"indicators\s+of\s+compromise|"
    r"global\s*eyes|"
    r"(?:activate|wake|boot|fire\s?up|spin\s?up|stand\s?down|"
    r"power\s?down|shut\s?down|kill)\s+(?:the\s+)?"
    r"(?:global\s*eyes|intel|intelligence|cti|opencti)"
    r")\b",
    re.I,
)

_CTI_COMMANDS_GUIDE = """\
Available OpenCTI commands (use `command` and `args`):

  cti_search        {"query": "<term>", "limit": 10}
  cti_add_observable {"value": "<observable value>",
                      "observable_type": "domain"|"ip"|"ipv6"|"url"
                                       |"email"|"md5"|"sha1"|"sha256"
                                       |"hash"|"file"|"user-agent"
                                       |"mutex"|"registry-key"}
  cti_create_incident {"name": "<incident name>",
                       "description": "<free text, optional>"}
  cti_link          {"from_id": "<stix id>", "to_id": "<stix id>",
                     "relationship": "related-to"|"indicates"
                                   |"attributed-to"|"uses"|"targets"
                                   |"mitigates"|"based-on"}
  cti_summary       {"hours": 24}
  cti_list_indicators {"limit": 10}
  cti_enrich        {"value": "<observable value>",
                     "observable_type": "domain"|"ip"|"ipv6"|"url"
                                       |"email"|"md5"|"sha1"|"sha256"
                                       |"hash"|"file"|"user-agent"
                                       |"mutex"|"registry-key"}
                    Add the observable AND wait ~30s for enrichment
                    connectors (VirusTotal, AbuseIPDB, Shodan, etc.)
                    to fire, then report the findings.
  cti_open_panel    {"dashboard": "<slug>", "path": "<optional URL path>"}
  cti_spinup        {}     # boot OpenCTI on Railway (~3 min cold start)
  cti_spindown      {}     # tear it down to stop the Railway bill"""


_CTI_OPEN_RE = re.compile(
    r"\b(open|show|bring up|pop up|put up|launch|display)\b[^.]*"
    r"\b(?:intel|intelligence|cti|opencti|threat\s+panel|threats?\s+dashboard)\b",
    re.I,
)


async def _route_cti(text: str) -> dict | None:
    """LLM-routed OpenCTI intent. Returns structured command or None.
    Mirrors `_route_desktop()` with a CTI-focused prompt."""
    client = _get_router_client()
    if client is None or not text:
        return None
    system = (
        "You translate ONE spoken request into ONE command for the user's "
        "self-hosted OpenCTI intelligence platform. Output strict JSON "
        "only — no prose, no code fences.\n\n"
        "Schema. Either:\n"
        '  {"cti": false}    — when the request is NOT about threat '
        "intelligence / OpenCTI / investigations.\n"
        "Or:\n"
        '  {"cti": true, "command": "<name>", "args": {...}, '
        '"say": "<one short butler-voice sentence>"}\n\n'
        f"{_CTI_COMMANDS_GUIDE}\n\n"
        "Rules:\n"
        "- For 'log foo.com as a suspicious domain' → cti_add_observable.\n"
        "- For 'enrich X' / 'look up X' / 'is X malicious' → cti_enrich.\n"
        "- For 'open the intel panel' / 'show the threats dashboard' → "
        "cti_open_panel.\n"
        "- For 'activate / wake / spin up Global Eyes' → cti_spinup.\n"
        "- For 'stand down / power down Global Eyes' → cti_spindown.\n"
        "- `say` is one short butler-voice sentence.\n"
        '- If unclear, output {"cti": false}.'
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cti router LLM failed: %s", exc)
        return None
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cti router non-JSON: %s — %r", exc, raw[:200])
        return None
    if not isinstance(data, dict) or not data.get("cti"):
        return None
    cmd = (data.get("command") or "").strip()
    if not cmd:
        return None
    return {
        "cmd": cmd,
        "args": data.get("args") or {},
        "say": (data.get("say") or "").strip(),
    }


_OBSERVABLE_TYPE_MAP: dict[str, str] = {
    "domain": "Domain-Name", "domain-name": "Domain-Name",
    "hostname": "Hostname",
    "ip": "IPv4-Addr", "ipv4": "IPv4-Addr",
    "ipv6": "IPv6-Addr",
    "url": "Url",
    "email": "Email-Addr", "email-addr": "Email-Addr",
    "md5": "StixFile", "sha1": "StixFile", "sha256": "StixFile",
    "hash": "StixFile", "file": "StixFile",
    "user-agent": "User-Agent",
    "mutex": "Mutex",
    "registry-key": "Windows-Registry-Key",
}


class OpenCTIClient:
    """Async GraphQL client for a Railway-hosted OpenCTI."""

    def __init__(self, bridge: "DesktopBridge") -> None:
        self._bridge = bridge
        self._base_url = os.environ.get("OPENCTI_URL", "").rstrip("/")
        self._token = os.environ.get("OPENCTI_TOKEN", "")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _ensure(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is not None:
                return self._client
            if not (self._base_url and self._token):
                raise RuntimeError(
                    "OpenCTI unavailable — OPENCTI_URL / OPENCTI_TOKEN "
                    "are not set on the worker"
                )
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, connect=8.0),
            )
            logger.info("opencti: client initialised for %s", self._base_url)
            return self._client

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        client = await self._ensure()
        resp = await client.post(
            "/graphql",
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"opencti errors: {data['errors'][:2]}")
        return data.get("data") or {}

    async def search(self, query: str, limit: int = 10) -> dict:
        gql = """
        query Search($search: String, $count: Int) {
          stixCoreObjects(search: $search, first: $count) {
            edges {
              node {
                id
                entity_type
                representative { main secondary }
              }
            }
          }
        }
        """
        data = await self._gql(gql, {"search": query, "count": limit})
        edges = (data.get("stixCoreObjects") or {}).get("edges") or []
        return {
            "matches": [
                {
                    "id": (e.get("node") or {}).get("id"),
                    "type": (e.get("node") or {}).get("entity_type"),
                    "name": (
                        ((e.get("node") or {}).get("representative") or {})
                        .get("main") or ""
                    ),
                }
                for e in edges
            ],
            "query": query,
        }

    async def add_observable(self, value: str, raw_type: str) -> dict:
        obs_type = _OBSERVABLE_TYPE_MAP.get(
            (raw_type or "").strip().lower(), raw_type or ""
        )
        if not obs_type:
            raise RuntimeError(f"unknown observable type '{raw_type}'")
        type_to_key: dict[str, str] = {
            "Domain-Name": "DomainName", "Hostname": "Hostname",
            "IPv4-Addr": "IPv4Addr", "IPv6-Addr": "IPv6Addr",
            "Url": "Url", "Email-Addr": "EmailAddr",
            "StixFile": "StixFile", "User-Agent": "UserAgent",
            "Mutex": "Mutex",
            "Windows-Registry-Key": "WindowsRegistryKey",
        }
        key = type_to_key.get(obs_type, obs_type.replace("-", ""))
        if obs_type == "StixFile":
            inner: dict = {
                "name": value,
                "hashes": [{"algorithm": "Unknown", "hash": value}],
            }
        elif obs_type == "Windows-Registry-Key":
            inner = {"attribute_key": value}
        else:
            inner = {"value": value}
        gql = """
        mutation AddObs($input: StixCyberObservableAddInput!) {
          stixCyberObservableAdd(input: $input) {
            id
            observable_value
            entity_type
          }
        }
        """
        data = await self._gql(
            gql, {"input": {"type": obs_type, key: inner}}
        )
        obs = data.get("stixCyberObservableAdd") or {}
        return {
            "id": obs.get("id"),
            "value": obs.get("observable_value") or value,
            "type": obs.get("entity_type") or obs_type,
        }

    async def create_incident(self, name: str, description: str = "") -> dict:
        gql = """
        mutation IncAdd($input: IncidentAddInput!) {
          incidentAdd(input: $input) { id name }
        }
        """
        data = await self._gql(
            gql, {"input": {"name": name, "description": description}}
        )
        inc = data.get("incidentAdd") or {}
        return {"id": inc.get("id"), "name": inc.get("name")}

    async def link(self, from_id: str, to_id: str, rel: str = "related-to") -> dict:
        gql = """
        mutation RelAdd($input: StixCoreRelationshipAddInput!) {
          stixCoreRelationshipAdd(input: $input) { id relationship_type }
        }
        """
        data = await self._gql(
            gql,
            {"input": {"fromId": from_id, "toId": to_id, "relationship_type": rel}},
        )
        r = data.get("stixCoreRelationshipAdd") or {}
        return {
            "id": r.get("id"),
            "relationship": r.get("relationship_type") or rel,
        }

    async def summary(self, hours: int = 24) -> dict:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        gql = """
        query Summary($filters: FilterGroup) {
          stixCoreObjects(
            filters: $filters, first: 8,
            orderBy: created_at, orderMode: desc
          ) {
            pageInfo { globalCount }
            edges { node { entity_type representative { main } } }
          }
        }
        """
        filters = {
            "mode": "and",
            "filters": [
                {"key": "created_at", "operator": "gt", "values": [cutoff]}
            ],
            "filterGroups": [],
        }
        data = await self._gql(gql, {"filters": filters})
        block = data.get("stixCoreObjects") or {}
        total = (block.get("pageInfo") or {}).get("globalCount", 0)
        edges = block.get("edges") or []
        return {
            "window_hours": hours,
            "total": total,
            "recent": [
                {
                    "type": (e.get("node") or {}).get("entity_type"),
                    "name": (
                        ((e.get("node") or {}).get("representative") or {})
                        .get("main", "")
                    ),
                }
                for e in edges
            ],
        }

    async def list_indicators(self, limit: int = 10) -> dict:
        gql = """
        query Inds($count: Int) {
          indicators(first: $count, orderBy: created_at, orderMode: desc) {
            edges { node { id name x_opencti_score } }
          }
        }
        """
        data = await self._gql(gql, {"count": limit})
        edges = (data.get("indicators") or {}).get("edges") or []
        return {
            "indicators": [
                {
                    "id": (e.get("node") or {}).get("id"),
                    "name": (e.get("node") or {}).get("name"),
                    "score": (e.get("node") or {}).get("x_opencti_score"),
                }
                for e in edges
            ],
        }

    async def get_observable(self, obs_id: str) -> dict:
        gql = """
        query GetObs($id: String!) {
          stixCyberObservable(id: $id) {
            id observable_value entity_type x_opencti_score
            externalReferences {
              edges { node { source_name url description } }
            }
            objectLabel { value color }
          }
        }
        """
        data = await self._gql(gql, {"id": obs_id})
        obs = data.get("stixCyberObservable") or {}
        refs = (obs.get("externalReferences") or {}).get("edges") or []
        labels = obs.get("objectLabel") or []
        return {
            "id": obs.get("id"),
            "value": obs.get("observable_value"),
            "type": obs.get("entity_type"),
            "score": obs.get("x_opencti_score"),
            "refs": [
                {
                    "source": (e.get("node") or {}).get("source_name"),
                    "url": (e.get("node") or {}).get("url"),
                    "description": (
                        (e.get("node") or {}).get("description") or ""
                    )[:200],
                }
                for e in refs
            ],
            "labels": [
                {"value": lab.get("value"), "color": lab.get("color")}
                for lab in labels
            ],
        }

    async def enrich(
        self, value: str, obs_type: str, deadline_s: float = 30.0
    ) -> dict:
        added = await self.add_observable(value, obs_type)
        obs_id = added.get("id")
        if not obs_id:
            return {"error": "could not create observable", **added}
        end = time.time() + deadline_s
        attempts = 0
        last: dict = {}
        while time.time() < end:
            attempts += 1
            try:
                last = await self.get_observable(obs_id)
            except Exception:  # noqa: BLE001
                pass
            if last.get("refs") or last.get("labels") or last.get("score"):
                last["enrichment_attempts"] = attempts
                return last
            await asyncio.sleep(5.0)
        if not last:
            last = added
        last["enrichment_attempts"] = attempts
        last["timed_out"] = True
        return last


def _cti_reply(cmd: str, args: dict, res: dict) -> str:
    """Format an OpenCTI result into a single butler sentence."""
    if not isinstance(res, dict) or res.get("error"):
        err = res.get("error") if isinstance(res, dict) else "no response"
        return f"OpenCTI hiccup, sir — {err}"
    if cmd == "cti_search":
        matches = res.get("matches") or []
        if not matches:
            q = args.get("query", "that")
            return f"Nothing in the intel for '{q}', sir."
        head = ", ".join(
            f"{m.get('name','?')} ({m.get('type','')})"
            for m in matches[:5]
        )
        return f"{len(matches)} hits, sir — {head}."
    if cmd == "cti_add_observable":
        return (
            f"Logged {args.get('value', res.get('value', 'it'))} as a "
            f"{args.get('observable_type', res.get('type', 'observable'))}, sir."
        )
    if cmd == "cti_create_incident":
        return (
            f"Incident '{args.get('name', res.get('name', 'unnamed'))}' "
            "is on the books, sir."
        )
    if cmd == "cti_link":
        rel = args.get("relationship") or res.get("relationship") or "related-to"
        return f"Linked, sir — {rel}."
    if cmd == "cti_summary":
        total = res.get("total", 0)
        hours = res.get("window_hours", args.get("hours", 24))
        if not total:
            return f"Nothing new in the last {hours} hours, sir."
        recent = res.get("recent") or []
        names = ", ".join(
            r.get("name", r.get("type", "?")) for r in recent[:3]
        )
        return (
            f"{total} new objects in the last {hours} hours, sir — "
            f"latest: {names}."
        )
    if cmd == "cti_list_indicators":
        items = res.get("indicators") or []
        if not items:
            return "No recent indicators, sir."
        return (
            f"{len(items)} recent indicators, sir — "
            + ", ".join(i.get("name", "?") for i in items[:4])
            + "."
        )
    if cmd == "cti_enrich":
        value = args.get("value") or res.get("value") or "that"
        refs = res.get("refs") or []
        labels = res.get("labels") or []
        score = res.get("score")
        if not refs and not labels and score is None:
            if res.get("timed_out"):
                return (
                    f"Logged {value}, sir, but no enrichment came back "
                    "in the window — the connectors may be cold."
                )
            return f"Logged {value}, sir. No findings yet."
        bits: list[str] = []
        if score is not None:
            bits.append(f"score {score}")
        sources = []
        for r in refs:
            s = r.get("source") or ""
            if s and s not in sources:
                sources.append(s)
            if len(sources) >= 3:
                break
        if sources:
            bits.append("flagged by " + ", ".join(sources))
        if labels:
            label_words = [lab.get("value", "") for lab in labels[:3]]
            label_words = [w for w in label_words if w]
            if label_words:
                bits.append("labels " + " / ".join(label_words))
        summary = "; ".join(bits) if bits else "no clear verdict"
        return f"{value} — {summary}, sir."
    if cmd == "cti_open_panel":
        return "Intelligence panel is up, sir."
    if cmd == "cti_spinup":
        return (
            "On it, sir — Global Eyes coming online, give me about three "
            "minutes."
        )
    if cmd == "cti_spindown":
        return "Powering down Global Eyes, sir."
    return "Done, sir."


_CTI_IDLE_SECONDS = 180


def _is_offline_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (502, 503, 504)
    msg = str(exc).lower()
    return any(
        tok in msg
        for tok in (
            "connect", "connection refused", "no route",
            "name or service not known", "temporarily unavailable",
            "502", "503", "504",
        )
    )


class GitHubDispatchClient:
    """Fires a workflow_dispatch on the OpenCTI lifecycle workflow."""

    def __init__(self) -> None:
        self._token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
        self._owner = os.environ.get("GITHUB_DISPATCH_OWNER", "gelson12")
        self._repo = os.environ.get(
            "GITHUB_DISPATCH_REPO", "friday_jarvis2"
        )
        self._workflow = os.environ.get(
            "GITHUB_DISPATCH_WORKFLOW", "opencti-lifecycle.yml"
        )
        self._ref = os.environ.get("GITHUB_DISPATCH_REF", "main")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> httpx.AsyncClient | None:
        async with self._lock:
            if self._client is not None:
                return self._client
            if not self._token:
                logger.warning(
                    "gh dispatch: GITHUB_DISPATCH_TOKEN not set; OpenCTI "
                    "lifecycle commands will fail until you add it"
                )
                return None
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=httpx.Timeout(15.0, connect=10.0),
            )
            logger.info(
                "gh dispatch: ready (%s/%s :: %s on %s)",
                self._owner, self._repo, self._workflow, self._ref,
            )
            return self._client

    async def dispatch(self, action: str) -> tuple[bool, str]:
        if action not in ("start", "stop"):
            return False, f"invalid action '{action}'"
        client = await self._ensure()
        if client is None:
            return False, "GITHUB_DISPATCH_TOKEN is not set on the worker"
        try:
            resp = await client.post(
                f"/repos/{self._owner}/{self._repo}/actions/workflows/"
                f"{self._workflow}/dispatches",
                json={"ref": self._ref, "inputs": {"action": action}},
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"gh dispatch network error: {exc}"
        if resp.status_code == 204:
            return True, ""
        return False, (
            f"gh dispatch returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        self._desktop = DesktopBridge()
        self._mobile = MobileBridge()
        # OpenCTI — Railway-hosted, reached via direct httpx. Lifecycle
        # (spinup/spindown) goes through GitHub Actions workflow_dispatch
        # on the opencti-lifecycle workflow in this same repo.
        self._cti = OpenCTIClient(self._desktop)
        self._gh = GitHubDispatchClient()
        # CTI lifecycle state — see _cti_spinup, _cti_idle_watch.
        self._cti_up: bool = False
        self._cti_last_active: float = 0.0
        self._cti_idle_task: asyncio.Task | None = None
        # Wake/sleep: dormant on connect, woken by "Hey Friday".
        self._awake = False
        # First-wake greeting includes bridge presence so the user knows
        # which of their machines I can drive.
        self._announced_status = False
        # Latest frame + capture time per source.
        self._cam_frame: rtc.VideoFrame | None = None
        self._cam_at: float = 0.0
        self._screen_frame: rtc.VideoFrame | None = None
        self._screen_at: float = 0.0
        self._seen_cam = False
        self._seen_screen = False
        self._video_tasks: set[asyncio.Task] = set()
        # Live remote-browser widget (Phase 4)
        self._browser = None
        self._browser_task: asyncio.Task | None = None
        # HUD widget inventory pushed from the frontend on jarvis-ui-state.
        # Stays [] until the first publish lands. `_open_widgets_at == 0.0`
        # means "no state ever received" — close-widget priority falls back
        # to permissive mode in that gap.
        self._open_widgets: list[dict] = []
        self._open_widgets_at: float = 0.0
        # Generic clarification slot for ambiguous intents.
        self._pending_clarification: PendingClarification | None = None
        self._clarification_resumers: dict[str, callable] = {}
        self._clarification_resumers["volume"] = self._resume_volume
        # APK build is its own one-shot follow-up (free-form repo answer
        # doesn't fit the option-picker shape that PendingClarification
        # uses). 30s TTL enforced inside the handler.
        self._apk_awaiting_repo: bool = False
        self._apk_awaiting_repo_at: float = 0.0
        self._apk_build_active: bool = False
        # Vault-feedback tracking: the previous turn went to Hermes (LLM path
        # wasn't intercepted by a local intent handler) at this monotonic
        # timestamp.  The next on_user_turn_completed inspects the user's
        # reply for accept/correct signals so the Hermes /v1/feedback endpoint
        # can mark the corresponding vault entry — closing the
        # self-improvement loop (Phase 3 of the vault-aware routing plan).
        self._last_llm_turn_at: float = 0.0
        self._feedback_url: str | None = None
        _hurl = (os.environ.get("HERMES_URL") or "").strip().rstrip("/")
        if _hurl:
            self._feedback_url = f"{_hurl}/v1/feedback"
        self._hermes_key: str = (os.environ.get("HERMES_API_KEY") or "").strip()
        # Accommodation booking. Lazy-init: build once on first use so a
        # missing LITEAPI_KEY doesn't crash worker startup. Pending book holds
        # a locked-in quote between "book the Marriott" and the user's "yes".
        self._accommodation = None  # type: ignore[assignment]
        self._accommodation_init_attempted = False
        self._accommodation_last_results: list = []
        self._accommodation_pending_book: dict | None = None
        # Pending search keeps a multi-turn conversation alive when the
        # first turn was missing a required slot. See
        # _maybe_resume_accommodation_search.
        self._accommodation_pending_search: dict | None = None
        self._wire_video(room)

    def _has_widget(self, kind: str) -> bool:
        """True when a panel of `kind` is currently visible on the HUD."""
        if not kind:
            return False
        target = kind.lower()
        return any(
            (w.get("kind") or "").lower() == target for w in self._open_widgets
        )

    # ── Vault feedback (Phase 3 of the self-improvement loop) ────────
    # If the previous turn went through Hermes (LLM fall-through), the
    # user's current reply can mark that interaction accepted or corrected.
    # Hermes patches the vault entry's metadata, which feeds the n8n
    # maturity cron, which over time downgrades repeat-domain calls to
    # cheaper models.  All best-effort — feedback failure never blocks chat.
    _NEG_FEEDBACK_RE = re.compile(
        r"^\s*(?:no[,. ]|nope|actually|wait|that'?s wrong|that is wrong|"
        r"that'?s not|i meant|i didn'?t|wrong|incorrect|stop)\b",
        re.IGNORECASE,
    )
    _POS_FEEDBACK_RE = re.compile(
        r"^\s*(?:thanks|thank you|perfect|great|nice|awesome|exactly|"
        r"that'?s right|brilliant|cheers)\b",
        re.IGNORECASE,
    )
    _FEEDBACK_TTL_S = 45.0

    def _maybe_emit_vault_feedback(self, text: str) -> None:
        if not self._feedback_url or not text:
            return
        last = self._last_llm_turn_at
        if not last or (time.time() - last) > self._FEEDBACK_TTL_S:
            return

        signal: str | None = None
        if self._NEG_FEEDBACK_RE.match(text):
            signal = "corrected"
        elif self._POS_FEEDBACK_RE.match(text):
            signal = "accepted"
        if signal is None:
            return

        # Consume the previous turn so we don't double-fire on the next utterance.
        self._last_llm_turn_at = 0.0

        session_id = self._room.name if self._room else ""
        if not session_id:
            return

        async def _post() -> None:
            headers: dict[str, str] = {}
            if self._hermes_key:
                headers["Authorization"] = f"Bearer {self._hermes_key}"
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    await client.post(
                        self._feedback_url,
                        json={"session_id": session_id, "signal": signal, "note": text[:120]},
                        headers=headers,
                    )
                logger.info("vault feedback posted: session=%s signal=%s", session_id, signal)
            except Exception as exc:
                logger.debug("vault feedback post failed: %s", exc)

        asyncio.create_task(_post())

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

    # ── Screen widget tools (floating HUD panels) ───────────────────
    async def _publish_ui(self, msg: dict) -> None:
        """Send a UI command to the browser on the `jarvis-ui` topic."""
        try:
            await self._room.local_participant.publish_data(
                json.dumps(msg).encode("utf-8"),
                reliable=True,
                topic=_UI_TOPIC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("jarvis-ui publish failed: %s", exc)

    async def _osiris_voice_alert_loop(self) -> None:
        """Speak OSIRIS criticals aloud. Gated by env (off by default):

          OSIRIS_VOICE_ALERTS=1   enable
          OSIRIS_URL=<base>       OSIRIS instance to subscribe to

        Subscribes to OSIRIS's SSE stream and, for each newly-seen entity with
        threat HIGH/CRITICAL, says one concise line. Reconnects forever. Speaks
        immediately on arrival (no blocking awaits) per the voice-latency rule.
        """
        enabled = (os.environ.get("OSIRIS_VOICE_ALERTS") or "").strip().lower()
        if enabled not in ("1", "true", "yes", "on"):
            return
        base = (os.environ.get("OSIRIS_URL") or "").rstrip("/")
        if not base:
            return
        url = f"{base}/api/sdk/stream"
        seen: set[str] = set()
        try:
            import aiohttp
        except Exception:  # noqa: BLE001
            logger.warning("OSIRIS voice alerts: aiohttp unavailable")
            return
        logger.info("OSIRIS voice alerts: subscribing to %s", url)
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        url, timeout=aiohttp.ClientTimeout(total=None, sock_read=120)
                    ) as r:
                        async for raw in r.content:
                            line = raw.decode("utf-8", "ignore").strip()
                            if not line.startswith("data:"):
                                continue
                            try:
                                msg = json.loads(line[5:].strip())
                            except Exception:  # noqa: BLE001
                                continue
                            if msg.get("type") != "entity_update":
                                continue
                            for e in msg.get("payload") or []:
                                threat = str(e.get("threat", "")).upper()
                                if threat not in ("HIGH", "CRITICAL"):
                                    continue
                                eid = str(e.get("id", ""))
                                if not eid or eid in seen:
                                    continue
                                seen.add(eid)
                                if len(seen) > 1000:
                                    seen.clear()
                                    seen.add(eid)
                                name = e.get("name") or "an event"
                                try:
                                    await self.session.say(
                                        f"Sir, OSIRIS flags a {threat.lower()} alert: {name}."
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("OSIRIS alert loop reconnecting: %s", exc)
                await asyncio.sleep(10)

    async def _maybe_handle_camera(self, text: str) -> bool:
        """Voice-controlled camera enable/disable. Publishes a structured
        JSON command on the `ui-command` topic; the frontend hook turns
        the track on/off. Returns True when handled (caller stops the
        turn). Mirrors OpenJarvis behaviour.
        """
        want = _camera_intent(text)
        if want is None:
            return False
        try:
            await self._room.local_participant.publish_data(
                json.dumps({"type": "camera", "enabled": want}).encode("utf-8"),
                reliable=True,
                topic=UI_COMMAND_TOPIC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ui-command camera publish failed: %s", exc)
            return False
        try:
            await self.session.say(
                "Camera on, sir." if want else "Camera off, sir."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_gesture_mode(self, text: str) -> bool:
        """Voice-controlled gesture mode. ON also forces camera ON; OFF
        also forces camera OFF — mirrors OpenJarvis. The fullscreen
        mirrored camera overlay on the frontend is gated on the
        gesture_mode flag flipped by this command.
        """
        want = _gesture_mode_intent(text)
        if want is None:
            return False
        try:
            # Camera state piggybacks gesture mode. Send camera FIRST
            # so the recogniser's track-availability gate flips before
            # the frontend tries to render the fullscreen preview.
            await self._room.local_participant.publish_data(
                json.dumps({"type": "camera", "enabled": want}).encode("utf-8"),
                reliable=True,
                topic=UI_COMMAND_TOPIC,
            )
            await self._room.local_participant.publish_data(
                json.dumps({"type": "gesture_mode", "enabled": want}).encode("utf-8"),
                reliable=True,
                topic=UI_COMMAND_TOPIC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ui-command gesture_mode publish failed: %s", exc)
            return False
        try:
            await self.session.say(
                "Gesture mode on, sir." if want else "Gesture mode off, sir."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_widget(self, text: str) -> None:
        """Open/close screen widgets when the user asks.

        Best-effort fallback so panels still work even if the LLM
        doesn't emit a show_widget tool call. Safe to double-fire —
        widgets are singletons on the browser side.
        """
        if not text:
            return
        opening = bool(_UI_OPEN_RE.search(text))
        closing = bool(_UI_CLOSE_RE.search(text))
        if not (opening or closing):
            return
        if closing and re.search(
            r"\b(all|everything|every (widget|panel)|the (widgets|panels))\b",
            text,
            re.I,
        ):
            await self._publish_ui({"type": "close_all"})
            return
        kind = _widget_from_text(text)
        if kind is None:
            return
        if closing and not opening:
            await self._publish_ui({"type": "close_widget", "kind": kind})
        else:
            await self._publish_ui({"type": "open_widget", "kind": kind})

    # ── Close-widget priority ────────────────────────────────────────
    async def _maybe_handle_close_widget(self, text: str) -> bool:
        """Close a HUD panel that the user just named.

        Runs BEFORE _maybe_handle_content so "close the YouTube" closes
        the panel instead of triggering a YouTube search for the word
        "Close." Guarded by the live widget inventory: only intercepts
        when the named widget is actually visible, so unrelated phrases
        ("close the YouTube tab in my browser") still reach the content
        and desktop routers.
        """
        if not text:
            return False
        if not _UI_CLOSE_RE.search(text):
            return False
        # An utterance with BOTH open- and close-verbs (e.g. "open chat,
        # close YouTube" in a long sentence) is too ambiguous for this
        # priority path — let _maybe_handle_widget see it.
        if _UI_OPEN_RE.search(text):
            return False
        # "Close all panels" — short-circuit, matches the existing
        # _maybe_handle_widget behaviour.
        if re.search(
            r"\b(all|everything|every (widget|panel)|the (widgets|panels))\b",
            text,
            re.I,
        ):
            await self._publish_ui({"type": "close_all"})
            try:
                await self.session.say("Closing all panels, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        kind = _widget_from_text(text)
        if kind is None:
            return False
        # Permissive fallback: if the frontend has never reported its
        # widget inventory (old build, browser closed), assume the user
        # means the HUD panel — preserves current behaviour.
        known_state = self._open_widgets_at > 0.0
        if known_state and not self._has_widget(kind):
            return False
        await self._publish_ui({"type": "close_widget", "kind": kind})
        try:
            await self.session.say(f"Closing the {kind} panel, sir.")
        except Exception:  # noqa: BLE001
            pass
        return True

    # ── Clarification resolver ───────────────────────────────────────
    async def _maybe_resume_clarification(self, text: str) -> bool:
        """If a clarification is pending, try to resolve `text` to an option.

        Returns True when the turn has been handled (option chosen, or
        cancelled). Returns False when there is nothing pending or the
        pending question has expired; in both cases the caller's normal
        routing proceeds.
        """
        pc = self._pending_clarification
        if pc is None:
            return False
        now = time.monotonic()
        if now >= pc.expires_at:
            logger.info("clarification expired: %s", pc.intent_kind)
            self._pending_clarification = None
            return False
        if not text:
            return False
        # Explicit cancel always wins.
        if _CANCEL_RE.search(text):
            self._pending_clarification = None
            try:
                await self.session.say("As you wish, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        low = text.lower()
        chosen: list[dict] = []
        # 1) "both" / "all" — bulk select when the intent supports it.
        if pc.intent_kind == "volume" and _BOTH_RE.search(text):
            chosen = list(pc.options)
        # 2) Word match — each option carries match_words.
        if not chosen:
            for opt in pc.options:
                words = opt.get("match_words") or []
                for w in words:
                    if re.search(rf"\b{re.escape(w)}\b", low, re.I):
                        chosen = [opt]
                        break
                if chosen:
                    break
        # 3) Ordinal fallback.
        if not chosen:
            for pattern, idx in _ORDINAL_RE:
                if pattern.search(text) and idx < len(pc.options):
                    chosen = [pc.options[idx]]
                    break
        if not chosen:
            logger.info(
                "clarification resume miss for %s: %r", pc.intent_kind, text[:120]
            )
            self._pending_clarification = None
            return False
        resumer = self._clarification_resumers.get(pc.intent_kind)
        if resumer is None:
            logger.warning(
                "no resumer registered for clarification kind %s", pc.intent_kind
            )
            self._pending_clarification = None
            return False
        captured = pc
        self._pending_clarification = None
        try:
            for opt in chosen:
                await resumer(text, captured, opt)
        except Exception as exc:  # noqa: BLE001
            logger.error("clarification resumer for %s failed: %s",
                         captured.intent_kind, exc)
        return True

    # ── Volume disambiguation ────────────────────────────────────────
    async def _maybe_handle_volume(self, text: str) -> bool:
        """Volume command with HUD/desktop disambiguation.

        Runs BEFORE _maybe_handle_content and _maybe_handle_desktop. If
        the user explicitly said "on my laptop" we defer to the existing
        desktop router (which has the LLM router + full machine handling).
        Otherwise we enumerate every plausible target — open HUD widgets
        that produce audio, plus active per-app audio sessions on each
        online bridge — and either dispatch (1 source), refuse politely
        (0 sources), or ask a clarification (>1 sources).
        """
        if not text or not _VOLUME_INTENT_RE.search(text):
            return False

        low = text.lower()
        # Refuse the desktop-machine bail when the user clearly named a
        # HUD audio widget. Without this, "unmute the news from the
        # YouTube window" got mis-routed to the desktop handler (the
        # word "from" is in `_DESKTOP_MACHINE_RE`'s preposition set),
        # which then said "I can't reach the laptop" because no machine
        # was online — the worst kind of wrong answer (volume IS
        # handled, just on the HUD side).
        hud_widget_kw = bool(re.search(
            r"\b("
            r"youtube|the\s+video|video\s+(?:panel|widget|window)|"
            r"music\s+(?:panel|widget|window)|"
            r"browser\s+(?:panel|widget|window)|"
            r"news\s+(?:panel|widget|window)|"
            r"(?:the\s+)?(?:panel|widget|window)"
            r")\b",
            low,
        ))
        if _DESKTOP_MACHINE_RE.search(text) and not hud_widget_kw:
            # User pinned the target machine — let the existing
            # desktop router (LLM + regex) handle it.
            return False

        # Resolve the action — mirrors _desktop_intent's branching.
        if re.search(r"\bunmute\b", low):
            action_args: dict = {"action": "unmute"}
        elif re.search(r"\bmute\b", low):
            action_args = {"action": "mute"}
        elif _VOL_SET.search(low) and re.search(
            r"\b(volume|sound|audio)\b", low
        ):
            level = int(_VOL_SET.search(low).group(1))
            action_args = {"action": "set", "level": max(0, min(100, level))}
        elif _VOL_DOWN.search(low):
            action_args = {"action": "down"}
        elif _VOL_UP.search(low):
            action_args = {"action": "up"}
        else:
            # Caught by _VOLUME_INTENT_RE but no direction parseable.
            return False

        # Master-volume opt-in word — only added when user explicitly
        # asks for "master" / "everything" / "all sound".
        wants_master = bool(
            re.search(r"\b(master|all\s+sound|everything)\b", low)
        )

        # ── Build the candidate list ─────────────────────────────────
        candidates: list[dict] = []

        # HUD audio widgets (visible panels that can produce sound).
        audio_widget_kinds = {"youtube", "music", "browser"}
        for w in self._open_widgets:
            wkind = (w.get("kind") or "").lower()
            if wkind not in audio_widget_kinds:
                continue
            title = (w.get("title") or wkind).strip() or wkind
            match_words = {wkind, title.lower()}
            if wkind == "youtube":
                match_words.update({"youtube", "video", "the video", "panel"})
            elif wkind == "music":
                match_words.update({"music", "the music", "panel"})
            elif wkind == "browser":
                match_words.update({"browser", "the browser", "panel"})
            candidates.append({
                "label": title if wkind != "youtube" else "YouTube",
                "target_kind": "widget",
                "widget_kind": wkind,
                "match_words": sorted(match_words),
            })

        # Per-app audio sessions on each online bridge. Run enumeration
        # in parallel so a slow bridge doesn't add seconds to the turn.
        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("control-room connect failed: %s", exc)
        machines = self._desktop.online_machines()
        if machines:
            async def _enum(m: str) -> tuple[str, dict]:
                try:
                    res = await self._desktop.send(
                        m, "audio_sessions", {}, timeout=4.0
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("audio_sessions on %s failed: %s", m, exc)
                    res = {"sessions": []}
                return m, res

            try:
                results = await asyncio.gather(
                    *(_enum(m) for m in machines), return_exceptions=False
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("audio enumeration failed: %s", exc)
                results = []

            for machine, res in results:
                for s in (res.get("sessions") or []):
                    if not s.get("is_active"):
                        continue
                    proc = (s.get("process_name") or "").strip()
                    if not proc:
                        continue
                    label = (s.get("display_name") or proc).strip() or proc
                    match_words = set(_derive_match_words(proc))
                    match_words.add(label.lower())
                    candidates.append({
                        "label": label,
                        "target_kind": "session",
                        "machine": machine,
                        "process_name": proc,
                        "match_words": sorted(w for w in match_words if w),
                    })

        # Master volume — only when explicitly requested.
        if wants_master and machines:
            for machine in machines:
                candidates.append({
                    "label": f"the {machine} master volume",
                    "target_kind": "master",
                    "machine": machine,
                    "match_words": ["master", "everything", "all"],
                })

        # ── Branch on candidate count ────────────────────────────────
        if not candidates:
            try:
                await self.session.say("Nothing is currently playing, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True

        if len(candidates) == 1:
            await self._dispatch_volume(candidates[0], action_args)
            return True

        # Multi-source: build a sayable prompt, store the pending state.
        # Cap at 3 options to keep the spoken question short.
        shown = candidates[:3]
        labels = [c["label"] for c in shown]
        if len(candidates) > 3:
            tail = f", {labels[-1]}, or another one"
            prompt = ", ".join(labels[:-1]) + tail + ", sir?"
        elif len(labels) == 3:
            prompt = f"{labels[0]}, {labels[1]}, or {labels[2]}, sir?"
        else:
            prompt = f"{labels[0]} or {labels[1]}, sir?"
        self._pending_clarification = PendingClarification(
            intent_kind="volume",
            options=candidates,
            original_args=action_args,
            prompt=prompt,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + 30.0,
        )
        try:
            await self.session.say(prompt)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _dispatch_volume(self, option: dict, action_args: dict) -> None:
        """Apply a parsed volume action to one resolved option."""
        target_kind = option.get("target_kind")
        action = action_args.get("action") or ""
        if target_kind == "widget":
            msg: dict = {
                "type": "widget_volume",
                "kind": option["widget_kind"],
                "action": action if action in ("mute", "unmute", "set") else (
                    # Map up/down to a coarse set — the IFrame API has
                    # absolute set only. The publish-side handler reads
                    # `level`, so derive one from the verb.
                    "set"
                ),
            }
            if action == "set":
                msg["level"] = int(action_args.get("level") or 50)
            elif action == "up":
                msg["level"] = 100
            elif action == "down":
                msg["level"] = 25
            await self._publish_ui(msg)
        elif target_kind == "session":
            await self._desktop.send(
                option["machine"], "app_volume",
                {"process_name": option["process_name"], **action_args},
                timeout=8.0,
            )
        elif target_kind == "master":
            await self._desktop.send(
                option["machine"], "volume", action_args, timeout=8.0,
            )
        else:
            logger.warning("dispatch_volume: unknown target_kind %r", target_kind)
            return

        verb_map = {
            "mute": "Muting",
            "unmute": "Unmuting",
            "up": "Turning up",
            "down": "Lowering",
            "set": "Setting",
        }
        verb = verb_map.get(action, "Adjusting")
        try:
            await self.session.say(f"{verb} {option['label']}, sir.")
        except Exception:  # noqa: BLE001
            pass

    async def _resume_volume(
        self, text: str, pc: PendingClarification, option: dict
    ) -> None:
        """Finish a deferred volume action once an option is chosen."""
        await self._dispatch_volume(option, pc.original_args)

    async def _maybe_handle_content(self, text: str) -> bool:
        """Handle search / video / news / maps / browser intents by regex.

        Returns True when an intent was handled — the caller then stops
        the turn, since we speak our own confirmation. This is the
        reliable path: the web_search / search_youtube / show_news /
        show_map / open_browser methods are unreliable as LLM tools.
        """
        intent = _content_intent(text)
        if intent is None:
            return False
        kind, arg = intent
        dispatch = {
            "web": lambda: self.web_search(arg),
            "youtube": lambda: self.search_youtube(arg),
            "news": lambda: self.show_news(arg),
            "maps": lambda: self.show_map(arg),
            "browser": lambda: self.open_browser(
                arg or "https://www.google.com"
            ),
            "v0": lambda: self.build_website(arg),
        }
        if kind not in dispatch:
            return False
        try:
            # Bound the call so a hung provider can never freeze the
            # voice turn. v0.dev generations can run 30-60s; everything
            # else should be much faster.
            handler_timeout = 120.0 if kind == "v0" else 20.0
            reply = await asyncio.wait_for(
                dispatch[kind](), timeout=handler_timeout
            )
        except asyncio.TimeoutError:
            logger.error("content intent '%s' timed out", kind)
            reply = "That's taking too long, sir — try again in a moment."
        except Exception as exc:  # noqa: BLE001
            logger.error("content intent '%s' failed: %s", kind, exc)
            reply = "I couldn't complete that just now, sir."
        try:
            await self.session.say(reply)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _wake_greeting(self) -> str:
        """Spoken on the FIRST wake of a session — includes which of the
        user's PCs are online so they don't have to discover it later.
        FRIDAY-style: leads with the polite greeting, then a soft "quick
        system status" framing before naming what's offline."""
        if self._announced_status:
            return "Yes, sir?"
        self._announced_status = True
        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("control-room connect failed: %s", exc)
        online = self._desktop.online_machines()
        if not online:
            return (
                "At your service, sir. Just a quick system status — it "
                "seems there's currently no connectivity with the desktop "
                "or ROG bridge. A quick desktop-bridge\\run.bat on either "
                "machine and I'll be at the helm."
            )
        if len(online) == 1:
            other = "ROG" if online[0] == "laptop" else "laptop"
            return (
                f"At your service, sir. Quick system note — your "
                f"{online[0]} is online and ready; the {other} bridge "
                "is dark at the moment."
            )
        return (
            "At your service, sir. All bridges nominal — both your "
            "laptop and the ROG are online and at your command."
        )

    # ── CTI panel + lifecycle ───────────────────────────────────────

    async def _open_cti_panel(
        self, dashboard: str | None = None, path: str | None = None
    ) -> None:
        """Publish an `open_widget` for the cti panel on the jarvis-ui topic."""
        payload: dict = {}
        cti_url = os.environ.get("OPENCTI_URL", "").rstrip("/")
        if cti_url:
            payload["url"] = cti_url
        if path:
            payload["path"] = path
        if dashboard:
            payload["dashboard"] = dashboard
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "cti",
                "title": "Intelligence",
                "payload": payload,
            }
        )

    async def _maybe_handle_cti(self, text: str) -> bool:
        """Operate OpenCTI via the LLM intent router (Path Y).

        RETIRED — OpenCTI dropped (OSIRIS covers intelligence). Inert no-op so it
        never routes or spins up the container. The dead code below is kept until
        this legacy agent is retired."""
        return False
        if not text or not _CTI_HINT_RE.search(text):
            return False

        # Fast path — pure panel-open intent skips the LLM call.
        if _CTI_OPEN_RE.search(text):
            await self._open_cti_panel()
            try:
                await self.session.say("Intelligence panel is up, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True

        routed = await _route_cti(text)
        if routed is None:
            return False
        cmd = routed["cmd"]
        args = routed["args"] or {}
        say_hint = routed.get("say", "")

        # Lifecycle short-circuits (no auto-spinup wrapper).
        if cmd == "cti_spinup":
            await self._cti_spinup()
            return True
        if cmd == "cti_spindown":
            await self._cti_spindown()
            return True

        # Quick ack for ops that aren't info-rich.
        info_cmds = {
            "cti_search", "cti_summary", "cti_list_indicators", "cti_enrich"
        }
        if say_hint and cmd not in info_cmds:
            try:
                await self.session.say(say_hint)
            except Exception:  # noqa: BLE001
                pass

        async def _run() -> dict:
            if cmd == "cti_search":
                return await self._cti.search(
                    str(args.get("query", "")),
                    int(args.get("limit") or 10),
                )
            if cmd == "cti_add_observable":
                return await self._cti.add_observable(
                    str(args.get("value", "")),
                    str(args.get("observable_type", "")),
                )
            if cmd == "cti_create_incident":
                return await self._cti.create_incident(
                    str(args.get("name", "")),
                    str(args.get("description", "")),
                )
            if cmd == "cti_link":
                return await self._cti.link(
                    str(args.get("from_id", "")),
                    str(args.get("to_id", "")),
                    str(args.get("relationship") or "related-to"),
                )
            if cmd == "cti_summary":
                return await self._cti.summary(int(args.get("hours") or 24))
            if cmd == "cti_list_indicators":
                return await self._cti.list_indicators(
                    int(args.get("limit") or 10)
                )
            if cmd == "cti_enrich":
                return await self._cti.enrich(
                    str(args.get("value", "")),
                    str(args.get("observable_type", "")),
                )
            if cmd == "cti_open_panel":
                await self._open_cti_panel(
                    dashboard=args.get("dashboard"),
                    path=args.get("path"),
                )
                return {"opened": True}
            return {"error": f"unknown cti command '{cmd}'"}

        res: dict
        try:
            res = await _run()
        except Exception as exc:  # noqa: BLE001
            if _is_offline_error(exc):
                logger.info("cti %s offline (%s) — auto-spinup", cmd, exc)
                try:
                    await self.session.say(
                        "Bringing Global Eyes online first, sir — one moment."
                    )
                except Exception:  # noqa: BLE001
                    pass
                up = await self._cti_spinup(quiet=True)
                if up:
                    try:
                        res = await _run()
                    except Exception as exc2:  # noqa: BLE001
                        logger.error("cti %s retry failed: %s", cmd, exc2)
                        res = {"error": f"retry after spinup failed: {exc2}"}
                else:
                    res = {"error": "auto-spinup did not complete in time"}
            else:
                logger.error("cti %s failed: %s", cmd, exc)
                res = {"error": str(exc)}

        if isinstance(res, dict) and not res.get("error"):
            self._cti_last_active = time.time()

        try:
            await self.session.say(_cti_reply(cmd, args, res))
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _cti_spinup(self, quiet: bool = False) -> bool:
        """Trigger workflow_dispatch start, then poll until healthy."""
        ok, why = await self._gh.dispatch("start")
        if not ok:
            try:
                await self.session.say(
                    f"I couldn't kick off the spin-up, sir — {why}"
                )
            except Exception:  # noqa: BLE001
                pass
            return False
        if not quiet:
            try:
                await self.session.say(
                    "On it, sir — Global Eyes coming online, give me "
                    "about three minutes."
                )
            except Exception:  # noqa: BLE001
                pass
        healthy = await self._wait_until_cti_healthy()
        if healthy:
            self._cti_up = True
            self._cti_last_active = time.time()
            if self._cti_idle_task is None or self._cti_idle_task.done():
                self._cti_idle_task = asyncio.create_task(
                    self._cti_idle_watch()
                )
            try:
                if not quiet:
                    await self.session.say("Global Eyes are online, sir.")
                await self._open_cti_panel()
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                await self.session.say(
                    "Global Eyes didn't come up in time, sir — check the "
                    "Railway dashboard."
                )
            except Exception:  # noqa: BLE001
                pass
        return healthy

    async def _cti_spindown(self) -> None:
        """Trigger workflow_dispatch stop + clean up local state."""
        self._cti_up = False
        if self._cti_idle_task is not None:
            self._cti_idle_task.cancel()
            self._cti_idle_task = None
        ok, why = await self._gh.dispatch("stop")
        if not ok:
            try:
                await self.session.say(
                    f"Couldn't fire the spin-down, sir — {why}"
                )
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            await self._publish_ui(
                {"type": "close_widget", "kind": "cti"}
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.session.say("Powering down Global Eyes, sir.")
        except Exception:  # noqa: BLE001
            pass

    async def _wait_until_cti_healthy(
        self, deadline_s: float = 420.0
    ) -> bool:
        """Poll OpenCTI's GraphQL ping every 10s up to ``deadline_s``."""
        end = time.time() + deadline_s
        attempts = 0
        while time.time() < end:
            attempts += 1
            try:
                await self._cti._gql("{ me { name } }")
                logger.info("cti healthy after %d probes", attempts)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("cti probe %d not yet healthy: %s", attempts, exc)
            await asyncio.sleep(10.0)
        return False

    async def _cti_idle_watch(self) -> None:
        """Tear OpenCTI down after _CTI_IDLE_SECONDS of inactivity."""
        try:
            while self._cti_up:
                await asyncio.sleep(30.0)
                if not self._cti_up:
                    return
                idle = time.time() - self._cti_last_active
                if idle >= _CTI_IDLE_SECONDS:
                    minutes = max(1, int(_CTI_IDLE_SECONDS // 60))
                    word = "minute" if minutes == 1 else "minutes"
                    try:
                        await self.session.say(
                            f"Global Eyes has been idle for over "
                            f"{minutes} {word}, sir — powering down to "
                            "save computational resources."
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    await self._cti_spindown()
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("cti idle watch crashed: %s", exc)

    async def _maybe_handle_desktop(self, text: str) -> bool:
        """Operate the user's Windows machines via the LLM intent router.

        Pipeline: broad keyword gate → LLM router (free-form → structured
        bridge command) → regex fallback if the router is unavailable →
        presence check → execute. Honest "bridge offline" message when
        the target machine isn't in the control room.
        """
        if not text or not _DESKTOP_HINT_RE.search(text):
            return False

        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("control-room connect failed: %s", exc)
        online = self._desktop.online_machines()

        routed = await _route_desktop(text, online)
        say_hint = ""
        if routed is not None:
            machine, cmd, args = routed["machine"], routed["cmd"], routed["args"]
            say_hint = routed.get("say", "")
        else:
            intent = _desktop_intent(text)
            if intent is None:
                return False
            machine, cmd, args = intent

        if machine != "all" and not self._desktop.is_online(machine):
            msg = (
                f"My apologies, sir — it appears your {machine}'s bridge "
                "is offline at the moment. A quick desktop-bridge\\run.bat "
                "on that machine and we'll be back in business."
            )
            if online:
                msg += f" Still online for you: {', '.join(online)}."
            try:
                await self.session.say(msg)
            except Exception:  # noqa: BLE001
                pass
            return True

        info_cmds = {
            "list_dir", "read_file", "system_status", "list_processes",
            "search_files", "host_info",
        }
        if say_hint and cmd not in info_cmds:
            try:
                await self.session.say(say_hint)
            except Exception:  # noqa: BLE001
                pass

        timeout = 70.0 if cmd == "shell" else 30.0
        try:
            res = await self._desktop.send(
                machine, cmd, args, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("desktop send failed: %s", exc)
            res = {"error": str(exc)}
        try:
            await self.session.say(_desktop_reply(machine, cmd, args, res))
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_mobile(self, text: str) -> bool:
        """Operate the user's Android phone via the mobile-bridge APK.

        Pipeline: broad hint gate → LLM router (free-form → structured
        command) → optional contacts_search resolve → bridge.send →
        speak butler-tone reply.
        """
        if not text or not _MOBILE_HINT_RE.search(text):
            return False

        try:
            await self._mobile._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("mobile control-room connect failed: %s", exc)
        online = self._mobile.online_phones()
        routed = await _route_mobile(text, online)
        if routed is None:
            return False

        if not online:
            try:
                await self.session.say(
                    "Your phone bridge isn't connected, sir — open Jarvis "
                    "Mobile Bridge and connect."
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        phone = routed["phone"] if routed.get("phone") != "any" else online[0]
        cmd = routed["cmd"]
        args = dict(routed.get("args") or {})

        # Two-step contact resolution: if the LLM gave us a contact_query
        # instead of a number for a send/dial intent, look it up first.
        contact_query = routed.get("contact_query", "")
        if (
            cmd in ("sms_send", "dial", "whatsapp_send")
            and not args.get("number")
            and contact_query
        ):
            search = await self._mobile.send(
                phone, "contacts_search",
                {"query": contact_query, "limit": 3},
                timeout=10.0,
            )
            contacts = search.get("contacts") or []
            if not contacts:
                try:
                    await self.session.say(
                        f"I couldn't find a contact for '{contact_query}', sir."
                    )
                except Exception:  # noqa: BLE001
                    pass
                return True
            args["number"] = contacts[0]["number"]

        say_hint = routed.get("say", "")
        if say_hint:
            try:
                await self.session.say(say_hint)
            except Exception:  # noqa: BLE001
                pass

        try:
            res = await self._mobile.send(phone, cmd, args, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            logger.error("mobile send failed: %s", exc)
            res = {"error": str(exc)}
        try:
            await self.session.say(_mobile_reply(phone, cmd, args, res))
        except Exception:  # noqa: BLE001
            pass
        return True

    # ── Accommodation booking ────────────────────────────────────────
    def _accommodation_service(self):
        """Lazy-init the accommodation service. Returns None when no provider
        env vars are configured — caller speaks a graceful "not configured"
        reply."""
        if self._accommodation is not None:
            return self._accommodation
        if self._accommodation_init_attempted or not _ACCOMMODATION_AVAILABLE:
            return None
        self._accommodation_init_attempted = True
        try:
            self._accommodation = AccommodationService.from_env()
        except Exception as exc:  # noqa: BLE001
            logger.warning("accommodation init failed: %s", exc)
            self._accommodation = None
        return self._accommodation

    async def _handle_accommodation_search(self, text: str) -> bool:
        service = self._accommodation_service()
        if service is None:
            try:
                await self.session.say("Accommodation isn't configured, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        # Combine with any prior in-progress turn so follow-up info
        # ("Somewhere in Lisbon" after "find me a hotel") parses with
        # full context.
        prior = self._accommodation_pending_search or {}
        combined_text = ((prior.get("prior_text") or "") + " " + (text or "")).strip()
        location = _accommodation_nlu.parse_location(combined_text)
        if not location:
            self._accommodation_pending_search = {
                "prior_text": combined_text,
                "created_at": time.time(),
            }
            try:
                await self.session.say(
                    "Where would you like to stay, sir? Tell me a city or area."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        check_in, check_out = _accommodation_nlu.parse_dates(combined_text)
        guests = _accommodation_nlu.parse_guests(combined_text)
        preferred = _accommodation_nlu.parse_provider_preference(combined_text)
        # Slots filled — clear pending so a fresh "find me a hotel" later
        # starts clean.
        self._accommodation_pending_search = None
        query = _AccommodationSearchQuery(
            location=location,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            currency=os.environ.get("ACCOMMODATION_DEFAULT_CURRENCY", "GBP"),
            preferred_providers=preferred,
        )
        # Speak BEFORE awaiting a slow provider call so voice never goes
        # silent. Apify Airbnb is the slow one (30-90s cold).
        is_apify_query = preferred == ["apify_airbnb"]
        try:
            await self.session.say(
                "Just a moment, sir — looking into that for you."
                if not is_apify_query
                else "Just a moment, sir — Airbnb takes a little longer to search."
            )
        except Exception:  # noqa: BLE001
            pass
        # Worker timeout must exceed APIFY_AIRBNB_TIMEOUT_S (default 60s).
        try:
            properties = await asyncio.wait_for(service.search(query, limit=12), timeout=90.0)
        except asyncio.TimeoutError:
            try:
                await self.session.say("The search took too long, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("accommodation search failed: %s", exc)
            try:
                await self.session.say("I couldn't reach the booking system just now, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        self._accommodation_last_results = properties
        await self._publish_ui({
            "type": "open_widget",
            "kind": "accommodation",
            "title": f"Stays in {location}",
            "payload": {
                "query": location,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "properties": [
                    {
                        "provider_id": p.provider_id,
                        "external_id": p.external_id,
                        "name": p.name,
                        "price_total": p.price_total,
                        "price_currency": p.price_currency,
                        "rating": p.rating,
                        "review_count": p.review_count,
                        "address": p.address,
                        "images": p.images[:3],
                        "lat": p.lat,
                        "lng": p.lng,
                    }
                    for p in properties
                ],
            },
        })
        # Mirror the stays onto the OSIRIS globe as gold pins (best-effort,
        # fire-and-forget so it never adds latency to the voice turn). No-ops
        # unless OSIRIS_URL is configured. See osiris_signals.py.
        try:
            from osiris_signals import make_entity, publish_signals
            pins = [
                make_entity(
                    id=f"stay-{p.provider_id}-{p.external_id}",
                    name=p.name,
                    lat=p.lat,
                    lng=p.lng,
                    color="#D4AF37",
                    price=p.price_total,
                    currency=p.price_currency,
                    provider=p.provider_id,
                )
                for p in properties
                if getattr(p, "lat", None) is not None and getattr(p, "lng", None) is not None
            ]
            if pins:
                asyncio.create_task(publish_signals("accommodation", pins))
        except Exception:  # noqa: BLE001
            pass
        if not properties:
            try:
                await self.session.say(
                    f"I couldn't find any properties in {location} for those dates, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        cheapest = properties[0]
        nights = (check_out - check_in).days
        try:
            await self.session.say(
                f"Found {len(properties)} properties in {location}, sir. "
                f"The {cheapest.name} is cheapest at {cheapest.price_total:.0f} "
                f"{cheapest.price_currency} total for {nights} nights. "
                f"Say 'book the {cheapest.name.split()[0]}' to reserve."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _handle_accommodation_book_start(self, text: str) -> bool:
        """Phase 2: locks in a quote and asks the user to confirm. The actual
        booking only fires once the user replies "yes" — handled in
        `_maybe_resume_accommodation_book` on the next turn."""
        service = self._accommodation_service()
        if service is None or not self._accommodation_last_results:
            try:
                await self.session.say(
                    "I don't have any properties to book, sir — search first."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        # Best-overlap property-name match.
        text_lower = text.lower()
        target = None
        best_overlap = 0
        for prop in self._accommodation_last_results:
            tokens = [t for t in prop.name.lower().split() if len(t) > 3]
            overlap = sum(1 for tok in tokens if tok in text_lower)
            if overlap > best_overlap:
                best_overlap = overlap
                target = prop
        if target is None:
            target = self._accommodation_last_results[0]
        first_name = os.environ.get("ACCOMMODATION_GUEST_FIRST_NAME", "").strip()
        last_name = os.environ.get("ACCOMMODATION_GUEST_LAST_NAME", "").strip()
        email = os.environ.get("ACCOMMODATION_GUEST_EMAIL", "").strip()
        if not (first_name and last_name and email):
            try:
                await self.session.say(
                    "Booking isn't fully set up, sir — guest details missing."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        is_redirect = target.extras.get("is_redirect_provider", False) if hasattr(target, "extras") else False
        if is_redirect:
            self._accommodation_pending_book = {
                "target": target,
                "quote": None,
                "is_redirect": True,
                "created_at": time.time(),
            }
            try:
                await self.session.say(
                    f"That's an Airbnb listing — I can open it on your phone so you "
                    f"finish the booking on Airbnb itself. Shall I send the link, sir?"
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        # Speak ahead of the quote so voice never goes silent during the
        # provider round-trip (LiteAPI prebook can take 5-10s).
        try:
            await self.session.say("One moment, sir — locking in the price.")
        except Exception:  # noqa: BLE001
            pass
        try:
            quote = await asyncio.wait_for(service.quote(target), timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            logger.error("accommodation quote failed: %s", exc)
            try:
                await self.session.say("I couldn't lock in the price just now, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        self._accommodation_pending_book = {
            "target": target,
            "quote": quote,
            "is_redirect": False,
            "created_at": time.time(),
        }
        try:
            await self.session.say(
                f"Locked in {quote.price_total:.0f} {quote.price_currency} total at "
                f"the {target.name}. {quote.cancellation_policy[:120]} "
                f"Confirm, sir?"
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _finalize_pending_book(self) -> bool:
        pending = self._accommodation_pending_book
        if not pending:
            return False
        self._accommodation_pending_book = None
        service = self._accommodation_service()
        if service is None:
            return True
        target = pending["target"]
        is_redirect = pending.get("is_redirect", False)
        quote = pending.get("quote")
        book_token = (quote.book_token if quote else target.book_token)
        request = _AccommodationBookingRequest(
            quote_id=(quote.quote_id if quote else target.book_token),
            book_token=book_token,
            guest_first_name=os.environ.get("ACCOMMODATION_GUEST_FIRST_NAME", "").strip(),
            guest_last_name=os.environ.get("ACCOMMODATION_GUEST_LAST_NAME", "").strip(),
            guest_email=os.environ.get("ACCOMMODATION_GUEST_EMAIL", "").strip(),
        )
        try:
            result = await asyncio.wait_for(
                service.book(
                    request,
                    property_name=target.name,
                    provider_id=target.provider_id,
                ),
                timeout=25.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("accommodation book failed: %s", exc)
            try:
                await self.session.say("The booking failed, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        if not result.success or not result.checkout_url:
            try:
                await self.session.say(
                    "The provider didn't return a payment link, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        if not service.telegram.configured:
            await self._publish_ui({
                "type": "open_widget",
                "kind": "accommodation",
                "title": "Complete on Airbnb" if is_redirect else "Complete payment",
                "payload": {
                    "query": target.name,
                    "checkout_url": result.checkout_url,
                    "price_total": result.price_total,
                    "price_currency": result.price_currency,
                },
            })
        try:
            if is_redirect:
                await self.session.say(
                    f"Sent the Airbnb listing to your phone, sir. "
                    f"Tap it to complete the booking on Airbnb."
                )
            else:
                await self.session.say(
                    f"Booking link sent to your phone, sir. "
                    f"Total {result.price_total:.0f} {result.price_currency}. "
                    f"Tap to complete payment securely."
                )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_resume_accommodation_book(self, text: str) -> bool:
        pending = self._accommodation_pending_book
        if not pending:
            return False
        if time.time() - pending["created_at"] > _ACCOMMODATION_PENDING_TTL_S:
            self._accommodation_pending_book = None
            return False
        if _ACCOMMODATION_NO_RE.search(text or ""):
            self._accommodation_pending_book = None
            try:
                await self.session.say("Cancelled, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        if _ACCOMMODATION_YES_RE.search(text or ""):
            return await self._finalize_pending_book()
        return False

    async def _maybe_handle_accommodation(self, text: str) -> bool:
        if not _ACCOMMODATION_RE.search(text or ""):
            return False
        if _ACCOMMODATION_BOOK_RE.search(text) and self._accommodation_last_results:
            return await self._handle_accommodation_book_start(text)
        return await self._handle_accommodation_search(text)

    async def _maybe_resume_accommodation_search(self, text: str) -> bool:
        """Continue an in-progress accommodation search when the previous
        turn was missing a required slot (typically location). Routes the
        current turn back to the search handler EVEN IF the keyword regex
        doesn't match."""
        pending = self._accommodation_pending_search
        if not pending:
            return False
        if time.time() - pending["created_at"] > 120:
            self._accommodation_pending_search = None
            return False
        if re.search(r"\b(weather|time|news|forget|never\s*mind|cancel|stop)\b", text or "", re.I):
            self._accommodation_pending_search = None
            return False
        return await self._handle_accommodation_search(text)

    async def _vault_write_note(self, rel_path: str, content: str) -> bool:
        """POST a note to the obsidian-mind Railway service. fj2 runs on
        Railway so a local FS write isn't an option; the cloud service has
        the vault mounted. Returns True on success."""
        url = os.environ.get("OBSIDIAN_MIND_URL", "").strip().rstrip("/")
        if not url:
            return False
        token = os.environ.get("OBSIDIAN_MIND_TOKEN", "").strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{url}/api/notes/{rel_path}",
                    json={"content": content},
                    headers=headers,
                )
                resp.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("vault write failed for %s: %s", rel_path, exc)
            return False

    async def _maybe_handle_telegram(self, text: str) -> bool:
        """Send a Telegram message via the official Bot API.

        Worker-side only — does NOT need the phone bridge to be online.
        Requires `TELEGRAM_BOT_TOKEN` env + `TELEGRAM_CONTACTS_JSON` env
        mapping `{"<name lowercased>": <chat_id>, ...}`.
        """
        if not text or not _TELEGRAM_RE.search(text):
            return False
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            try:
                await self.session.say(
                    "Telegram isn't configured, sir — the bot token is unset."
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        extracted = await _extract_telegram_payload(text)
        if not extracted:
            return False
        contact_name = extracted["contact"].lower().strip()
        message = extracted["message"].strip()

        try:
            contacts = json.loads(
                os.environ.get("TELEGRAM_CONTACTS_JSON", "{}") or "{}"
            )
        except Exception:  # noqa: BLE001
            contacts = {}
        chat_id = contacts.get(contact_name)
        if not chat_id:
            try:
                await self.session.say(
                    f"I don't have a Telegram chat id for '{contact_name}', sir."
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
                )
                r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram send failed: %s", exc)
            try:
                await self.session.say("Telegram didn't accept that, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True

        try:
            await self.session.say(f"Telegrammed {contact_name}, sir.")
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_apk_build(self, text: str) -> bool:
        """Voice-triggered Android APK build via inspiring-cat.

        Two flavours share this handler:

        1. Dedicated mobile-bridge ("rebuild the mobile bridge" /
           "compile the phone app") → always builds the mobile-bridge
           module in gelson12/friday_jarvis2; legacy tag prefix
           `mobile-bridge-v0.1.0-` (the auto-delete workflow already
           catches it).

        2. Generic ("build the apk from <owner>/<repo>") → clones the
           named GitHub repo, builds the root Android project, releases
           under tag prefix `voice-apk-<owner>-<repo>-<timestamp>` on
           gelson12/friday_jarvis2 (per user policy: single release
           host = single cleanup workflow = single PAT). If the user
           didn't name a repo, we set a one-shot follow-up flag and
           ask "which repo, sir?" — the next turn is consumed by
           `_maybe_handle_apk_repo_followup`.

        Guarded so a double-trigger doesn't fire two builds in parallel.
        """
        if not text or not _APK_BUILD_RE.search(text):
            return False
        if getattr(self, "_apk_build_active", False):
            try:
                await self.session.say(
                    "A build is already running, sir — I'll let you "
                    "know when it's done."
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        # Dedicated mobile-bridge shortcut — never asks for a repo.
        if _APK_MOBILE_BRIDGE_RE.search(text):
            await self._launch_apk_build(
                source_repo=MOBILE_BRIDGE_REPO,
                module_dir="mobile-bridge",
                tag_prefix="mobile-bridge-v0.1.0-",
                speak_name="mobile-bridge APK",
            )
            return True

        # Generic flow — try to extract a github owner/repo from the
        # utterance ("build the apk from gelson12/weather-app").
        m = _APK_REPO_RE.search(text)
        if m:
            owner, repo = m.group(1), m.group(2)
            await self._launch_generic_apk_build(owner, repo)
            return True

        # No repo named — ask back and consume the next user turn.
        self._apk_awaiting_repo = True
        self._apk_awaiting_repo_at = time.monotonic()
        try:
            await self.session.say(
                "Which repo, sir? I need owner slash name — for example, "
                "gelson12 slash weather-app."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_apk_repo_followup(self, text: str) -> bool:
        """One-shot follow-up consumer for "which repo, sir?".

        Runs BEFORE every other handler so a free-form "owner/repo"
        answer doesn't get routed to the content matcher. 30-second
        TTL; cancellable with "never mind".
        """
        if not getattr(self, "_apk_awaiting_repo", False):
            return False
        # TTL — drop the flag silently if the user moved on.
        if time.monotonic() - getattr(self, "_apk_awaiting_repo_at", 0.0) > 30:
            self._apk_awaiting_repo = False
            return False
        if not text:
            return False
        if _CANCEL_RE.search(text):
            self._apk_awaiting_repo = False
            try:
                await self.session.say("As you wish, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        m = _APK_REPO_BARE_RE.search(text)
        if not m:
            # Couldn't parse — leave the flag set so the user can try
            # again, but log the miss for tuning.
            logger.info("apk repo follow-up miss: %r", text[:120])
            try:
                await self.session.say(
                    "I didn't catch the repo, sir. Try again with "
                    "owner slash name."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        self._apk_awaiting_repo = False
        owner, repo = m.group(1), m.group(2)
        await self._launch_generic_apk_build(owner, repo)
        return True

    async def _launch_generic_apk_build(self, owner: str, repo: str) -> None:
        """Speak the ack + spawn the build worker for an arbitrary repo."""
        # `voice-apk-` prefix is what the cleanup workflow filters on
        # to delete arbitrary-repo builds after 24h. The owner+repo
        # slug is included for human-readable tag listings.
        slug = re.sub(r"[^a-z0-9]+", "-", f"{owner}-{repo}".lower()).strip("-")
        tag_prefix = f"voice-apk-{slug}-"
        await self._launch_apk_build(
            source_repo=f"{owner}/{repo}",
            module_dir="",   # build at repo root; user can override via env
            tag_prefix=tag_prefix,
            speak_name=f"APK from {owner} slash {repo}",
        )

    async def _launch_apk_build(
        self,
        *,
        source_repo: str,
        module_dir: str,
        tag_prefix: str,
        speak_name: str,
    ) -> None:
        """Set the active flag, speak the ack, and spawn the worker."""
        self._apk_build_active = True
        try:
            await self.session.say(
                f"Building the {speak_name} on inspiring-cat, sir — "
                "this takes about 10 to 15 minutes. I'll announce the "
                "download link when it's ready."
            )
        except Exception:  # noqa: BLE001
            pass
        asyncio.create_task(self._apk_build_worker(
            source_repo=source_repo,
            module_dir=module_dir,
            tag_prefix=tag_prefix,
        ))

    async def _latest_apk_tag(self, tag_prefix: str) -> str | None:
        """Return the latest release tag on APK_RELEASE_REPO matching prefix."""
        url = f"https://api.github.com/repos/{APK_RELEASE_REPO}/releases"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url, headers={"Accept": "application/vnd.github+json"}
                )
                resp.raise_for_status()
                for rel in resp.json():
                    tag = rel.get("tag_name") or ""
                    if tag.startswith(tag_prefix):
                        return tag
        except Exception as exc:  # noqa: BLE001
            logger.warning("apk-tag fetch failed: %s", exc)
        return None

    async def _apk_build_worker(
        self,
        *,
        source_repo: str,
        module_dir: str,
        tag_prefix: str,
    ) -> None:
        """Background task: submit build, poll for release, speak URL."""
        try:
            before = await self._latest_apk_tag(tag_prefix)
            logger.info(
                "apk build: starting source=%s module=%r prefix=%s before=%s",
                source_repo, module_dir, tag_prefix, before,
            )

            owner, _, name = source_repo.partition("/")
            source_url = f"https://github.com/{source_repo}.git"
            # The build script reads these env vars. APK_MODULE_DIR is
            # empty for repos whose Android project is at the root.
            env_exports = (
                f"export SOURCE_REPO_URL='{source_url}' "
                f"REPO_OWNER='{owner}' REPO_NAME='{name}' "
                f"APK_MODULE_DIR='{module_dir}' "
                f"TAG_PREFIX='{tag_prefix}' "
                f"RELEASE_REPO='{APK_RELEASE_REPO}';"
            )
            cmd = (
                f"nohup bash -c '{env_exports} curl -fsSL "
                f"{MOBILE_BRIDGE_BUILD_SCRIPT_URL} | bash "
                f"> /tmp/mb-build.log 2>&1' "
                f"> /tmp/mb-launcher.log 2>&1 < /dev/null & "
                f"disown $!; echo \"launched pid=$!\""
            )
            payload = {
                "type": "shell",
                "payload": {"command": cmd, "cwd": "/workspace"},
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"{INSPIRING_CAT_URL}/tasks", json=payload
                )
                r.raise_for_status()
                launch_resp = r.json()
            logger.info("apk build launcher accepted: %s", launch_resp)

            # Poll GitHub releases every 30 s for up to 25 min.
            deadline = time.monotonic() + 25 * 60
            new_tag: str | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(30)
                tag = await self._latest_apk_tag(tag_prefix)
                if tag and tag != before:
                    new_tag = tag
                    break

            if not new_tag:
                try:
                    await self.session.say(
                        "The APK build didn't finish within 25 minutes, "
                        "sir — check the inspiring-cat log."
                    )
                except Exception:  # noqa: BLE001
                    pass
                return

            release_url = (
                f"https://github.com/{APK_RELEASE_REPO}"
                f"/releases/tag/{new_tag}"
            )
            try:
                short = new_tag[len(tag_prefix):] or "build"
                await self.session.say(
                    f"Your APK is ready, sir — {short}. "
                    "The release page is on the panel. "
                    "It will be deleted in 24 hours."
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._publish_ui({
                    "type": "open_widget",
                    "kind": "site",
                    "title": f"APK: {new_tag}",
                    "payload": {
                        "url": release_url,
                        "prompt": f"APK build from {source_repo}",
                    },
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("apk site widget open failed: %s", exc)

        except Exception as exc:  # noqa: BLE001
            logger.error("apk build worker failed: %s", exc)
            try:
                await self.session.say(
                    "The APK build hit an error, sir — "
                    "check the inspiring-cat logs."
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._apk_build_active = False

    @function_tool
    async def show_widget(self, widget: str, title: str = "") -> str:
        """Display a floating widget panel on the user's JARVIS screen.

        Use this when the user asks to open, show, or bring up a panel.

        Args:
            widget: Which panel — one of "chat", "clock", "music",
                "search", "news", "youtube", "maps", "apps", "system".
            title: Optional custom header text for the panel.
        """
        kind = (widget or "").strip().lower()
        valid = {
            "chat", "clock", "music", "search", "news",
            "youtube", "maps", "browser", "apps", "system",
        }
        if kind not in valid:
            return (
                f"There is no '{widget}' widget. Available panels: "
                + ", ".join(sorted(valid))
            )
        msg: dict = {"type": "open_widget", "kind": kind}
        if title:
            msg["title"] = title
        await self._publish_ui(msg)
        return f"Displayed the {kind} widget on screen."

    @function_tool
    async def hide_widget(self, widget: str = "") -> str:
        """Close a floating widget on the user's JARVIS screen.

        Args:
            widget: Which panel to close. Pass "all" (or leave it
                empty) to clear every widget from the screen.
        """
        kind = (widget or "").strip().lower()
        if kind in ("", "all", "everything"):
            await self._publish_ui({"type": "close_all"})
            return "Cleared all widgets from the screen."
        await self._publish_ui({"type": "close_widget", "kind": kind})
        return f"Closed the {kind} widget."

    # ── Search & content tools (web, video, news, maps) ─────────────
    async def web_search(self, query: str) -> str:
        """Search the web and show the results on the JARVIS screen.

        Use this when the user asks to search for, google, or look up
        something on the web.

        Args:
            query: What to search the web for.
        """
        results = await search_tools.web_search(query, limit=6)
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "search",
                "title": f"Search — {query}",
                "payload": {"query": query, "results": results},
            }
        )
        if not results:
            return f"I couldn't find anything for '{query}', sir."
        return f"I found {len(results)} results for '{query}', now on screen."

    async def search_youtube(self, query: str) -> str:
        """Search YouTube and show the videos on the JARVIS screen.

        Args:
            query: What videos to search for.
        """
        videos = await search_tools.youtube_search(query, limit=8)
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "youtube",
                "title": f"YouTube — {query}",
                "payload": {"query": query, "videos": videos},
            }
        )
        if not videos:
            return f"No videos found for '{query}', sir."
        return (
            f"I found {len(videos)} videos for '{query}' — "
            "the first is ready to play."
        )

    async def build_website(self, prompt: str) -> str:
        """Generate a website via v0.dev and open it in a `site` widget.

        Two-utterance UX: speak an immediate acknowledgement so the user
        knows the command landed (v0 generation can take 30-60 s), then
        return the completion sentence which the caller TTSes.
        """
        prompt = (prompt or "").strip() or "a simple landing page"
        api_key = os.environ.get("V0_API_KEY", "").strip()
        if not api_key:
            return "I can't reach v0 — the V0_API_KEY isn't set, sir."

        # Immediate ack so the user knows we heard them. Fire-and-forget;
        # the spoken return from this method comes ~30-60 s later.
        try:
            await self.session.say(
                "Building your site, sir — one moment."
            )
        except Exception:  # noqa: BLE001
            pass

        payload = {
            "model": V0_MODEL,
            "messages": [
                {"role": "user", "content": f"Build a website: {prompt}"}
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=110.0) as client:
                resp = await client.post(
                    f"{V0_API_BASE}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("v0 generation failed: %s", exc)
            return (
                "v0 didn't respond just now, sir — try again in a moment."
            )

        content = ""
        try:
            content = (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            )
        except Exception:  # noqa: BLE001
            content = ""

        urls = _V0_PREVIEW_URL_RE.findall(content)
        # Prefer vusercontent.net (clean preview) over v0.dev/chat (editor).
        preview_url = next(
            (u for u in urls if "vusercontent.net" in u),
            urls[0] if urls else "",
        )
        if not preview_url:
            logger.warning(
                "v0 returned no preview URL; raw: %r", content[:300]
            )
            return (
                "I built something but couldn't find a preview link, sir."
            )

        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "site",
                "title": f"Site — {prompt[:40]}" if prompt else "Generated Site",
                "payload": {"url": preview_url, "prompt": prompt},
            }
        )
        return "Your site's on the panel, sir."

    async def show_news(self, topic: str = "") -> str:
        """Show current news headlines + a related news video, and speak
        a top-headline summary. Mirrors OpenJarvis.

        Args:
            topic: Optional subject (e.g. "technology"). Empty = top
                headlines + a generic "breaking news today" video.
        """
        topic = (topic or "").strip()

        try:
            articles = await search_tools.news_search(topic, limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.warning("news fetch failed: %s", exc)
            articles = []

        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "news",
                "title": f"News — {topic}" if topic else "Top Headlines",
                "payload": {"query": topic, "articles": articles},
            }
        )

        if not articles:
            return "I couldn't reach the news feed just now, sir."

        top = articles[0] if articles else {}
        top_title = (top.get("title") or "").strip()
        top_source = (top.get("source") or "").strip()
        where = f" on {topic}" if topic else ""

        # Derive a SAFE YouTube query from the top headline rather than
        # piping a generic "breaking news today" string (which only ever
        # returns evergreen filler). Headlines run 80-130 chars — far too
        # specific for YouTube — so strip the trailing source suffix
        # (" - Reuters"), pipe-delimited subtitle, and the punctuation
        # YouTube treats as zero-value, then keep the first 8 words. The
        # core noun phrase is almost always in the first 6-8 words; more
        # specificity kills recall.
        video_query = re.sub(r"\s+-\s+[A-Z][A-Za-z0-9 .&'-]+$", "", top_title)
        video_query = re.sub(r"\s+\|.*$", "", video_query).strip()
        video_query_clean = re.sub(r"[\"'`]", "", video_query)
        video_query_short = " ".join(video_query_clean.split()[:8])
        if not video_query_short:
            video_query_short = topic or "world news today"

        videos: list[dict] = []
        if len(video_query_short) >= 12:
            try:
                videos = await asyncio.wait_for(
                    search_tools.youtube_search(video_query_short, limit=8),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("news-companion youtube_search failed: %s", exc)

        if videos:
            async def _open_video_after_summary() -> None:
                try:
                    await asyncio.sleep(4.0)
                    await self._publish_ui(
                        {
                            "type": "open_widget",
                            "kind": "youtube",
                            "title": f"News Video — {video_query_short[:50]}",
                            "payload": {
                                "query": video_query_short,
                                "videos": videos,
                            },
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("delayed news-video open failed: %s", exc)
            asyncio.create_task(_open_video_after_summary())

        if top_title:
            lead = f"Top headline{where}: {top_title}"
            lead += f", from {top_source}." if top_source else "."
        else:
            lead = f"Latest headlines{where} on screen, sir."

        tail = (
            "Video coming up in a moment, sir."
            if videos
            else f"That's {len(articles)} headlines on screen."
        )
        return f"{lead} {tail}"

    async def show_map(self, place: str) -> str:
        """Show a place, address, or directions on a map on the JARVIS
        screen.

        Args:
            place: A place or address (e.g. "Tower Bridge, London"), or
                a directions query (e.g. "London to Oxford").
        """
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "maps",
                "title": f"Maps — {place}",
                "payload": {"query": place},
            }
        )
        return f"Showing {place} on the map, sir."

    # ── Live remote-browser widget (Phase 4) ────────────────────────
    async def _publish_browser(self, msg: dict) -> None:
        """Publish a frame chunk on the `jarvis-browser` data topic."""
        try:
            await self._room.local_participant.publish_data(
                json.dumps(msg).encode("utf-8"),
                reliable=True,
                topic="jarvis-browser",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("jarvis-browser publish failed: %s", exc)

    async def _push_browser_frame(self) -> None:
        """Screenshot the live page and stream it in ~12 KB chunks."""
        if self._browser is None:
            return
        img = await self._browser.screenshot()
        if img is None:
            return
        b64 = base64.b64encode(img).decode()
        frame_id = uuid.uuid4().hex[:8]
        size = 12000
        total = max(1, (len(b64) + size - 1) // size)
        for seq in range(total):
            msg: dict = {
                "t": "frame",
                "id": frame_id,
                "seq": seq,
                "total": total,
                "data": b64[seq * size : (seq + 1) * size],
            }
            if seq == 0:
                msg["url"] = self._browser.url
            await self._publish_browser(msg)

    async def _browser_stream_loop(self) -> None:
        """Refresh the streamed frame while the browser widget is open."""
        try:
            await asyncio.sleep(0.4)  # let the widget mount + subscribe
            while self._browser is not None:
                await self._push_browser_frame()
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error("browser stream loop failed: %s", exc)

    async def _stop_browser(self) -> None:
        """Cancel streaming and close the Playwright page, if any."""
        task, self._browser_task = self._browser_task, None
        if task is not None:
            task.cancel()
        browser, self._browser = self._browser, None
        if browser is not None:
            await browser.close()

    async def handle_browser_event(self, msg: dict) -> None:
        """Apply a relayed interaction from the browser widget."""
        if self._browser is None:
            return
        action = msg.get("action")
        if action == "click":
            await self._browser.click(
                float(msg.get("x", 0.0)), float(msg.get("y", 0.0))
            )
        elif action == "scroll":
            await self._browser.scroll(float(msg.get("dy", 0.0)))
        elif action == "navigate":
            await self._browser.navigate(str(msg.get("url", "")))
        elif action == "back":
            await self._browser.back()
        elif action == "reload":
            await self._browser.reload()
        elif action == "key":
            await self._browser.key(str(msg.get("key", "")))
        elif action == "close":
            await self._stop_browser()
            return
        else:
            return
        await self._push_browser_frame()

    async def open_browser(self, url: str = "https://www.google.com") -> str:
        """Open or navigate the live browser, reusing the session when possible.

        Returns immediately — Playwright runs in a background task so the
        voice turn does not stall ~5–15s waiting for Chromium to launch
        (which previously made Friday feel frozen).

        Args:
            url: The page to load. If a browser is already up this just
                navigates it (no relaunch). If the URL is empty / default
                and one is already open, asks where to go instead of
                stacking a second empty window on top.
        """
        target = (url or "").strip()
        is_default = target in ("", "https://www.google.com")

        # Reuse path — never relaunch Chromium when a browser is already up.
        if self._browser is not None:
            await self._publish_ui({"type": "focus_widget", "kind": "browser"})
            if is_default:
                return (
                    "The browser is already up, sir — "
                    "where would you like to go?"
                )
            # Background-navigate so the voice turn ends fast.
            asyncio.create_task(self._browser.navigate(target))
            return f"Going to {target}, sir."

        # Fresh open — kick Playwright off in the background.
        from browser_view import BrowserSession

        browser = BrowserSession()
        # Claim the slot BEFORE awaiting so a concurrent call sees the
        # session and takes the reuse path instead of double-launching.
        self._browser = browser
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "browser",
                "title": "Browser",
                "payload": {"loading": True},
            }
        )
        self._browser_task = asyncio.create_task(
            self._open_and_stream(browser, target or "https://www.google.com")
        )
        if is_default:
            return "Opening the browser, sir."
        return f"Opening the browser to {target}, sir."

    async def _open_and_stream(self, browser, url: str) -> None:
        """Background: load the page in Chromium, then stream frames."""
        try:
            await browser.open(url)
        except Exception as exc:  # noqa: BLE001
            logger.error("browser open failed: %s", exc)
            if self._browser is browser:
                self._browser = None
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
            return
        await self._browser_stream_loop()

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Wake/sleep gate, then vision injection.

        The session is always connected, so this fires on every spoken
        turn. While DORMANT we ignore everything except the wake phrase;
        `raise StopResponse()` cleanly drops the turn (no LLM reply).
        """
        text = getattr(new_message, "text_content", "") or ""

        # Vault feedback (Phase 3): if the previous turn went through Hermes
        # and the user is now correcting or affirming it, tell Hermes so the
        # vault entry gets the success_flag — fuels the maturity cron.
        try:
            self._maybe_emit_vault_feedback(text)
        except Exception:
            pass

        # Diagnostic: every transcribed user turn lands here. Logging the
        # raw text + current awake state + wake-regex match lets us debug
        # wake-word misses from the Railway deploy log without needing
        # browser DevTools.
        logger.info(
            "turn: awake=%s text=%r match=%s",
            self._awake,
            text,
            bool(_WAKE_RE.search(text)),
        )

        # ── Wake / sleep state machine ───────────────────────────────
        if not self._awake:
            if _WAKE_RE.search(text):
                self._awake = True
                logger.info("wake: triggered by %r", text)
                rest = _WAKE_RE.sub("", text).strip(" ,.!?-")
                if len(rest.split()) >= 2:
                    # "Hey Friday, what's the time" — answer the question.
                    # Update local text so downstream regex handlers see
                    # the clean query. Deliberately do NOT mutate
                    # new_message.content — livekit-agents fires
                    # preemptive LLM generation before this handler
                    # finishes, and mutating the message invalidates the
                    # speculative result (adding whole-RTT latency on
                    # every wake turn). Modern LLMs cope with the
                    # leading "Hey Friday," fine.
                    text = rest
                else:
                    await self.session.say(await self._wake_greeting())
                    raise StopResponse()
            else:
                # Dormant — ignore all non-wake speech silently.
                raise StopResponse()
        else:
            if _SLEEP_RE.search(text):
                self._awake = False
                await self.session.say("Goodbye, sir.")
                raise StopResponse()

        # ── Pending clarification — resolve before anything else ─────
        # If we asked a disambiguation question last turn, this turn is
        # the answer. Resolves to a target and dispatches the deferred
        # action; expires or cancels cleanly otherwise.
        if await self._maybe_resume_clarification(text):
            raise StopResponse()

        # ── APK build repo follow-up — consume "owner/repo" answer ───
        # Runs immediately after the generic clarification slot so a
        # free-form "owner slash name" reply to "which repo, sir?"
        # never falls through to the content matcher.
        if await self._maybe_handle_apk_repo_followup(text):
            raise StopResponse()

        # ── Volume — disambiguates across HUD widgets and desktop apps ─
        # Runs before content + desktop so "mute" never falls into the
        # YouTube content matcher and never needs "on my laptop".
        if await self._maybe_handle_volume(text):
            raise StopResponse()

        # ── Close-widget priority — beats content router when a panel ─
        # is actually open ("close the YouTube" → close panel, NOT search
        # YouTube for "Close"). Guarded by live widget inventory.
        if await self._maybe_handle_close_widget(text):
            raise StopResponse()

        # ── Accommodation booking — confirm resume (yes/no) ──────────
        # Runs BEFORE content so a bare "yes" after "confirm the Marriott?"
        # doesn't get swallowed by a search/show handler.
        if await self._maybe_resume_accommodation_book(text):
            raise StopResponse()

        # ── Accommodation search — multi-turn slot-fill continuation ──
        # Runs BEFORE content so "in Lisbon" after "find me a hotel" routes
        # back to the accommodation handler instead of being misread as
        # a search query.
        if await self._maybe_resume_accommodation_search(text):
            raise StopResponse()

        # ── Screen content — search / video / news / maps / browser ──
        # Regex fallback: the voice LLM does not reliably emit tool calls,
        # so detect the intent here and run the real flow. We speak our
        # own confirmation, so stop the turn before it reaches the LLM.
        if await self._maybe_handle_content(text):
            raise StopResponse()

        # ── Accommodation booking (LiteAPI Phase 1 + Apify Airbnb Phase 2) ──
        # Search/book hotels and Airbnb by voice. PCI-safe — card data never
        # touches the worker. See brain/Accommodation Booking — PCI &
        # Payment Handoff in the vault.
        if await self._maybe_handle_accommodation(text):
            raise StopResponse()

        # ── OpenCTI threat-intel: RETIRED ────────────────────────────
        # Dropped — OSIRIS covers the intelligence surface, the OpenCTI graph +
        # connectors were unused, and the per-session→global spin-up was a race
        # (audit J3). Dispatch removed; handler is inert below; the dead code is
        # excised when this legacy agent is retired.

        # ── Desktop control — operate the user's Windows machines ────
        if await self._maybe_handle_desktop(text):
            raise StopResponse()

        # ── Mobile control — operate the user's Android phone ────────
        if await self._maybe_handle_mobile(text):
            raise StopResponse()

        # ── Telegram (worker-side Bot API; no phone required) ────────
        if await self._maybe_handle_telegram(text):
            raise StopResponse()

        # ── APK build (voice-triggered rebuild of mobile-bridge) ─────
        # Fires the same /tasks shell pipeline used to produce the
        # first APK; runs ~10-15 min on inspiring-cat and publishes
        # the result as a GitHub release.
        if await self._maybe_handle_apk_build(text):
            raise StopResponse()

        # ── Screen widgets (open/close panels on request) ────────────
        await self._maybe_handle_widget(text)

        # ── Gesture mode ──────────────────────────────────────────────
        # Runs BEFORE the bare camera intent so phrasings like "turn on
        # the camera gesture mode" claim as gesture-mode (which enables
        # the camera as a side effect). Frontend overlay shows a
        # mirrored fullscreen camera for hand-gesture UI control.
        if await self._maybe_handle_gesture_mode(text):
            raise StopResponse()

        # ── Camera on/off ─────────────────────────────────────────────
        # Structured command to the browser via the `ui-command` topic.
        # Mirrors OpenJarvis. Camera intent only fires when the noun
        # (camera|cam|webcam|video) AND a polarity verb both match.
        if await self._maybe_handle_camera(text):
            raise StopResponse()

        # Reached LLM fall-through: this turn will be sent to Hermes (vision
        # injection below may add a frame, but the LLM call still happens).
        # Stamp the timestamp so the next user turn can post accept/correct
        # feedback against this Hermes interaction.
        self._last_llm_turn_at = time.time()

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

    assistant = Assistant(ctx.room)
    await session.start(
        room=ctx.room,
        agent=assistant,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
            # Required for the worker to receive the user's camera and
            # screen-share tracks (default is off → no video reaches us).
            video_enabled=True,
        ),
    )

    # Speak OSIRIS critical alerts aloud (no-op unless OSIRIS_VOICE_ALERTS=1
    # and OSIRIS_URL are set). Runs for the life of the session.
    asyncio.create_task(assistant._osiris_voice_alert_loop())

    # Relay live-browser interactions (click / scroll / key / navigate)
    # from the browser widget back to the worker's Playwright page.
    @ctx.room.on("data_received")
    def _on_browser_data(packet: rtc.DataPacket) -> None:
        if packet.topic != "jarvis-browser":
            return
        try:
            msg = json.loads(bytes(packet.data).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        asyncio.create_task(assistant.handle_browser_event(msg))

    # Reverse channel: the frontend pushes its open-widget inventory on
    # `jarvis-ui-state` so the worker can answer "is the YouTube panel
    # actually open?" — required by close-widget priority and volume
    # disambiguation.
    @ctx.room.on("data_received")
    def _on_ui_state(packet: rtc.DataPacket) -> None:
        if packet.topic != "jarvis-ui-state":
            return
        try:
            msg = json.loads(bytes(packet.data).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        if msg.get("type") == "widget_state":
            open_list = msg.get("open") or []
            assistant._open_widgets = open_list if isinstance(open_list, list) else []
            assistant._open_widgets_at = time.monotonic()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "widget_state: %d open (%s)",
                    len(assistant._open_widgets),
                    [w.get("kind") for w in assistant._open_widgets],
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
    # Name the worker explicitly so LiveKit Cloud can route dispatch
    # requests to it. The frontend's room-config sets
    # `agents: [{ agent_name: "friday" }]`, and LiveKit dispatches the
    # matching worker. Without a name on either side, a project with
    # "explicit dispatch only" mode (or no auto-dispatch rule) will
    # leave the worker registered but idle and the user stuck on the
    # welcome screen — which is what was happening on Railway after
    # the AGENT_NAME env var was cleared.
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=os.getenv("AGENT_NAME", "friday"),
        )
    )