import os
import re
import time
import json
import uuid
import base64
import asyncio
import logging
from dataclasses import dataclass, field
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


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        self._desktop = DesktopBridge()
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
        self._wire_video(room)

    def _has_widget(self, kind: str) -> bool:
        """True when a panel of `kind` is currently visible on the HUD."""
        if not kind:
            return False
        target = kind.lower()
        return any(
            (w.get("kind") or "").lower() == target for w in self._open_widgets
        )

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
        if _DESKTOP_MACHINE_RE.search(text):
            # User pinned the target machine — let the existing
            # desktop router (LLM + regex) handle it.
            return False

        low = text.lower()
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
        }
        if kind not in dispatch:
            return False
        try:
            # Bound the search/browser call so a hung provider can never
            # freeze the whole voice turn.
            reply = await asyncio.wait_for(dispatch[kind](), timeout=20.0)
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

    async def show_news(self, topic: str = "") -> str:
        """Show current news headlines + a related news video, and speak
        a top-headline summary. Mirrors OpenJarvis.

        Args:
            topic: Optional subject (e.g. "technology"). Empty = top
                headlines + a generic "breaking news today" video.
        """
        topic = (topic or "").strip()
        video_query = topic or "breaking news today"

        try:
            articles, videos = await asyncio.gather(
                search_tools.news_search(topic, limit=8),
                search_tools.youtube_search(video_query, limit=8),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("news+video parallel fetch failed: %s", exc)
            articles, videos = [], []

        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "news",
                "title": f"News — {topic}" if topic else "Top Headlines",
                "payload": {"query": topic, "articles": articles},
            }
        )

        if videos:
            async def _open_video_after_summary() -> None:
                try:
                    await asyncio.sleep(4.0)
                    await self._publish_ui(
                        {
                            "type": "open_widget",
                            "kind": "youtube",
                            "title": f"News Video — {video_query}",
                            "payload": {
                                "query": video_query,
                                "videos": videos,
                            },
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("delayed news-video open failed: %s", exc)
            asyncio.create_task(_open_video_after_summary())

        if not articles:
            return "I couldn't reach the news feed just now, sir."

        top = articles[0] if articles else {}
        top_title = (top.get("title") or "").strip()
        top_source = (top.get("source") or "").strip()
        where = f" on {topic}" if topic else ""

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

        # ── Screen content — search / video / news / maps / browser ──
        # Regex fallback: the voice LLM does not reliably emit tool calls,
        # so detect the intent here and run the real flow. We speak our
        # own confirmation, so stop the turn before it reaches the LLM.
        if await self._maybe_handle_content(text):
            raise StopResponse()

        # ── Desktop control — operate the user's Windows machines ────
        if await self._maybe_handle_desktop(text):
            raise StopResponse()

        # ── Screen widgets (open/close panels on request) ────────────
        await self._maybe_handle_widget(text)

        # ── Camera on/off ─────────────────────────────────────────────
        # Structured command to the browser via the `ui-command` topic.
        # Mirrors OpenJarvis. Camera intent only fires when the noun
        # (camera|cam|webcam|video) AND a polarity verb both match.
        if await self._maybe_handle_camera(text):
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