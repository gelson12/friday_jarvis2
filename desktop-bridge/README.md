# Jarvis Desktop Bridge

Lets the cloud-hosted Jarvis (friday_jarvis2 / OpenJarvis) operate your
**Windows machines — laptop AND ROG** — with **no tunnel, no public
hostname, no port-forwarding, no DNS.**

## Why this exists

A cloud server can't reach a PC behind a home router unless the PC is
exposed inbound (a tunnel) OR the PC holds an outbound connection. This
bridge takes the second path: it connects **outbound** to LiveKit Cloud
(exactly what your voice sessions already do) and joins a fixed control
room. The cloud agent drops a command in that room; the bridge runs it
locally and sends the result back. Your PC never accepts an inbound
connection.

## Setup — run on EACH machine (laptop, then ROG)

1. Copy this `desktop-bridge/` folder onto the machine.
2. Edit `run.bat`:
   - `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` — same LiveKit project as
     the voice agents (`jarvis-98rhrfmj`).
   - `JARVIS_MACHINE` — **`laptop`** on the laptop, **`rog`** on the ROG.
     This label is how the agent picks which machine to act on.
   - `JARVIS_BRIDGE_ALLOW_SHELL` — `1` to permit shell commands.
3. Double-click `run.bat`. First run creates a venv and installs deps.
   You should see: `connected to room 'jarvis-control' as
   desktop-bridge-laptop`.
4. To start it automatically at login: Task Scheduler → Create Task →
   Trigger "At log on" → Action: start `run.bat`.

Both machines join the same room (`jarvis-control`); they coexist. A
command's `target` (`laptop` / `rog` / `all`) decides who runs it.

## Commands the bridge executes

`host_info`, `open` (file/folder/app/URL), `list_dir`, `read_file`,
`write_file`, `make_dir`, `shell` (opt-in via `JARVIS_BRIDGE_ALLOW_SHELL`).

## Cloud side

The friday_jarvis2 worker (`agent.py`) exposes a `control_desktop`
tool. When you say e.g. *"Friday, open Notepad on my ROG"* the worker
publishes a command to `jarvis-control` targeted at `rog`; this bridge
runs it and replies. No env beyond `LIVEKIT_*` is needed on the cloud
side — it already has the LiveKit credentials.

## Security

Commands run with your Windows user privileges. Keep
`JARVIS_BRIDGE_ALLOW_SHELL=0` unless you need arbitrary shell. Anyone
who can publish to your LiveKit project's `jarvis-control` room can
drive these machines — keep `LIVEKIT_API_SECRET` private.
