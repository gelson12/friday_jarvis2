# 🤖 Branch Comparison — `br` ↔ `LiveKit`

> 💡 **TL;DR** — These aren't competing implementations. They're **two halves of the same product**: `br` is the *voice* (Python agent worker), `LiveKit` is the *face* (Next.js UI). They never call each other directly — they meet inside a LiveKit Cloud room.

---

## 🗺️ The architecture at a glance

```text
   📱 Browser / Phone
          │
          │  HTTPS                  ┌──────────────────────────────┐
          ├────────────────────────►│  ⚛️  LiveKit branch          │
          │                         │     Next.js 15 UI            │
          │   ◄── WebRTC (audio,    │     • POST /api/token (JWT)  │
          │       video, data) ────►│     • WebRTC client          │
          │                         │     • Service B on Railway   │
          │                         └──────────────┬───────────────┘
          │                                        │ mints JWT for
          │                                        │ project
          │                                        ▼
          │                  ┌──────────────────────────────────┐
          ├─────WebRTC──────►│  ☁️  LiveKit Cloud SFU            │
          │                  │     wss://jarvis-98rhrfmj…       │
          │                  └──────────────┬───────────────────┘
          │                                 │ auto-dispatch
          │                                 ▼
          │                  ┌──────────────────────────────────┐
          └────WebRTC───────►│  🐍  br branch                   │
                             │     Python voice agent           │
                             │     • livekit-agents worker      │
                             │     • STT → Hermes LLM → TTS     │
                             │     • Service A on Railway       │
                             └──────────────┬───────────────────┘
                                            │ HTTPS  /v1
                                            ▼
                             ┌──────────────────────────────────┐
                             │  🧠  Hermes service              │
                             │     (separate Railway service)   │
                             └──────────────────────────────────┘
```

---

## 📊 Side-by-side cheat sheet

| 🔍 Aspect | 🐍 `br` branch | ⚛️ `LiveKit` branch |
|---|---|---|
| 🎭 **Role** | Voice agent worker | Frontend + JWT minter |
| 💻 **Language** | Python 3.11 | TypeScript + React 19 |
| 📦 **Framework** | `livekit-agents` (Python SDK) | Next.js 15 (App Router) |
| 🚪 **Entry point** | `agent.py` → `agents.cli.run_app(...)` | `app/page.tsx` + `app/api/token/route.ts` |
| ⏳ **Process model** | Long-lived worker, room-per-job | Stateless HTTP, request-per-token |
| 🌐 **Public port** | ❌ none (outbound WS only) | ✅ `:3000` (Railway-assigned domain) |
| 🐳 **Container base** | `python:3.11-slim` (single-stage) | `node:20-alpine` (3-stage standalone) |
| 🏗️ **Build steps** | `pip install` + VAD pre-download | `pnpm install` → `pnpm build` → copy `.next/standalone` |
| ❄️ **Cold start** | Slower (Silero VAD load) | Faster (alpine + standalone server.js) |
| 🧠 **LLM** | ✅ `openai.LLM` → Hermes (`hermes-agent`) | ❌ none — UI never calls an LLM |
| 🎤 **STT** | ✅ Deepgram → Google fallback | ❌ none |
| 🔊 **TTS** | ✅ Google Cloud → Deepgram fallback (low-latency Aura) | ❌ none |
| 👂 **VAD** | ✅ Silero, prewarmed per worker process | ❌ n/a |
| 🛡️ **Noise cancel** | ✅ LiveKit BVC plugin | ❌ n/a |
| 🧭 **Agent dispatch** | Implicit — no `agent_name`, auto-dispatched | Mints token only; no dispatch logic |
| 🆔 **Session ID** | `ctx.room.name` → `X-Hermes-Session-Id` header | Random `voice_assistant_room_<n>` per click |
| 🔑 **Required env** | `LIVEKIT_*`, `HERMES_URL`, `HERMES_API_KEY`, `N8N_MCP_SERVER_URL?` | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| 💥 **Common failure** | "Connection error" when `HERMES_URL=localhost` | 500 on `/api/token` if `LIVEKIT_*` unset |
| ⚡ **Latency lever** | TTS provider (Deepgram Aura), VAD prewarm | `output:'standalone'`, preconnect buffer |
| 🔁 **Restart policy** | `on_failure`, retry 5× | `on_failure`, retry 5× |
| 🚢 **Railway service** | Service A (worker) | Service B (web) |

---

## 🐍 The `br` branch — the voice

### 📂 Code
A small Python file (`agent.py`, ~95 lines) wires LiveKit Agents to the STT/LLM/TTS stack. Personality lives in `prompts.py`. Optional `hermes_adapter.py` is a tiny HTTP client (kept around for non-LiveKit callers).

```python
session = AgentSession(
    vad=ctx.proc.userdata["vad"],       # Silero, prewarmed
    stt=stt,                             # Deepgram → Google
    llm=openai.LLM(                     # Hermes (OpenAI-compatible)
        model="hermes-agent",
        base_url=f"{hermes_url}/v1",
        api_key=hermes_key,
        extra_headers={"X-Hermes-Session-Id": ctx.room.name},
    ),
    tts=tts,                             # Google → Deepgram
)
```

### 🏗️ Infrastructure
- 🐳 **Dockerfile** pre-downloads the Silero VAD model at build time so worker boot doesn't pay for it.
- 🚂 **Railway** runs `python agent.py start` as a long-lived worker — **no port exposed**, no inbound HTTP.
- 🌍 **Networking** is purely outbound WebSocket to LiveKit Cloud + outbound HTTPS to Hermes/Deepgram/Google.

### 🧠 LLM + model handling
- Speaks to Hermes via the **OpenAI Chat Completions wire format** (Hermes exposes `/v1/...`).
- The model identifier `"hermes-agent"` is a *route key* on the Hermes side, not a literal model name — Hermes does its own routing/fallback to underlying providers.
- One LiveKit room → one Hermes session. The `X-Hermes-Session-Id` header pins memory/state.

### 👷 Worker handling
- `agents.cli.run_app(WorkerOptions(entrypoint_fnc, prewarm_fnc))` registers a **worker** with LiveKit Cloud.
- LiveKit Cloud auto-dispatches the worker into any new room (no `agent_name` set ⇒ automatic dispatch).
- `prewarm_fnc` loads heavy stuff (VAD) once per process; `entrypoint_fnc` runs per room.
- Worker is restart-on-failure (up to 5×).

### ✅ Pros
- 🎯 Tight, focused — one file does the voice loop.
- 🧘 Stateless from LiveKit's POV: any worker can pick up any room.
- 🔁 Provider-agnostic STT/TTS chains with graceful fallback.
- 💸 No public port → smaller attack surface, no CDN/edge needed.

### ⚠️ Cons / gotchas
- 🚨 `HERMES_URL=http://localhost:8642` is the committed default — **you MUST override it on Railway** to the internal `hermes-agent.railway.internal:<port>` URL, or every LLM call fails with `Connection error`.
- 🐢 Cold start carries the Silero VAD load even with build-time prewarm caching.
- 🧪 No HTTP healthcheck possible (no port) — you debug from Deploy Logs.
- 🔇 `unpinned livekit-agents` in `requirements.txt` — fresh builds can drift; pin if you need reproducibility.

---

## ⚛️ The `LiveKit` branch — the face

### 📂 Code
A Next.js 15 App Router app (the LiveKit `agent-starter-react` fork) rebranded as **Friday**: arc-reactor logo (`public/arc-reactor.svg`), CSS/SVG animated HUD background (`components/app/jarvis-hud-background.tsx`), forced dark theme, "Talk to Friday" CTA. UI behavior is configured in `app-config.ts`. The only server-side route is `app/api/token/route.ts`:

```ts
const at = new AccessToken(API_KEY, API_SECRET, {
  identity: participantIdentity,
  name: 'user',
  ttl: '15m',
});
at.addGrant({ room, roomJoin: true, canPublish: true,
              canPublishData: true, canSubscribe: true });
return NextResponse.json({ serverUrl, roomName, participantName, participantToken });
```

### 🏗️ Infrastructure
- 🐳 **Multi-stage Dockerfile** (alpine): `deps → build → runtime`. Runtime copies `.next/standalone` + `.next/static` + `public`, runs as non-root, `CMD ["node","server.js"]`.
- ⚙️ `next.config.ts` uses `output: 'standalone'` → minimal image, fast cold start.
- 📄 `.gitattributes` pins LF so Windows checkouts don't break the Prettier lint gate.
- 🚂 **Railway** exposes port 3000 (`PORT` env honored).

### 🧠 LLM + model handling
- **Nothing.** This branch never calls a language model. It only:
  1. Mints short-lived JWTs (`AccessToken`, 15-min TTL) using the LiveKit server SDK.
  2. Renders the WebRTC client.
- All "intelligence" lives in the `br` agent on the other side of the SFU.

### 👷 Worker handling
- **No worker concept.** Each `POST /api/token` is independent and stateless.
- No persistent connection from the Next.js server to LiveKit — the *browser* opens WebRTC to the SFU.
- "Automatic dispatch" here just means *don't put an agent name in the token's `RoomConfiguration`* — leave `AGENT_NAME` blank in env, and LiveKit Cloud assigns whichever worker is registered to the project.

### ✅ Pros
- 🎨 Fully customisable, beautiful UI without touching the agent.
- 🌍 Public URL → reachable from any device (phone test in the tutorial works directly).
- ⚡ Browser ↔ SFU media path is WebRTC; the Next.js server is only on the *control* path (token), not the audio path.
- 🧊 Stateless / horizontally scalable — add replicas to handle more token-mints.

### ⚠️ Cons / gotchas
- 🔓 The token route is **unauthenticated** — anyone who can hit `/api/token` can join. Fine for a demo / shared link, but add an auth layer before exposing publicly.
- 🪟 Without `.gitattributes` enforcing LF, Windows contributors get CRLF on checkout and `next build` fails Prettier (already fixed on this branch).
- 🧬 Frontend feature drift: `agent-starter-react` evolves quickly; the agent's `livekit-agents` SDK must stay current to register handlers for newer text-stream topics (`lk.agent.request`, `lk.chat`, etc.).
- ❌ Cannot test end-to-end without a running `br` worker on the **same** LiveKit project.

---

## 🔗 How they meet (the rendezvous)

1. Browser hits Service B → `POST /api/token` → JWT for project `jarvis-98rhrfmj`, random room name.
2. Browser opens WebRTC to LiveKit Cloud using that JWT.
3. LiveKit Cloud sees a participant joined and **auto-dispatches** any worker registered to the project (that's Service A on `br`).
4. The agent worker joins the room, runs the STT → LLM → TTS loop, publishes audio back to the SFU.
5. The browser hears the agent. The agent hears the browser. Neither ever called the other's URL.

The **only shared contract** is the LiveKit project: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` must match across both services.

---

## 🎯 When to edit which

| You want to change… | Touch the branch… |
|---|---|
| 🗣️ Voice personality, jokes, refusals | 🐍 `br` (`prompts.py`) |
| 🛠️ Tools the agent can call | 🐍 `br` (`agent.py` + MCP) |
| 🎚️ TTS / STT provider / voice | 🐍 `br` (`agent.py`) |
| 🧠 Which LLM / Hermes route | 🐍 `br` (env: `HERMES_URL`) |
| 🎨 Branding, logo, animations, theme | ⚛️ `LiveKit` (`app-config.ts`, `components/app/*`) |
| 🔐 Auth on the token endpoint | ⚛️ `LiveKit` (`app/api/token/route.ts`) |
| 📱 PWA / camera / chat UI | ⚛️ `LiveKit` (`components/agents-ui/*`) |
| 🔑 LiveKit credentials | 🤝 **Both** (must match) |
| 🚀 Deploy region / cold-start tuning | 🤝 **Both** (co-locate for minimum RTT) |

---

## 🧪 Quick "is it wired up?" checklist

- [ ] 🐍 `br` service Railway logs show `registered worker` (not just `failed to connect…`).
- [ ] 🐍 `br` env has `HERMES_URL` pointing at Hermes (NOT `localhost`).
- [ ] ⚛️ `LiveKit` service `POST /api/token` returns **200** with a JWT (not the old "INSECURE" 500).
- [ ] 🤝 Both services share the same `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.
- [ ] 🌍 Both services in the same Railway region (latency).
- [ ] 🗣️ Open the UI, click **Talk to Friday**, hear a reply within ~1–2 s.

---

🤖 *Maintained alongside the code on both branches. Edit on one, mirror on the other.*
