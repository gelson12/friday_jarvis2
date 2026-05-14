import os
import logging
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.agents import mcp
from livekit.plugins import openai, silero, noise_cancellation
from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION

load_dotenv()
logger = logging.getLogger(__name__)


def prewarm(proc: agents.JobProcess):
    """Pre-download VAD model once per worker process to avoid cold-start delay."""
    proc.userdata["vad"] = silero.VAD.load()


class Assistant(Agent):
    def __init__(self):
        super().__init__(instructions=AGENT_INSTRUCTION)


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    hermes_url = os.environ.get("HERMES_URL", "http://localhost:8642")
    hermes_key = os.environ.get("HERMES_API_KEY", "no-key")
    n8n_mcp_url = os.environ.get("N8N_MCP_SERVER_URL", "")

    mcp_servers = []
    if n8n_mcp_url:
        mcp_servers.append(mcp.MCPServerHTTP(url=n8n_mcp_url))

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=openai.STT(),
        llm=openai.LLM(
            model="hermes-agent",
            base_url=f"{hermes_url}/v1",
            api_key=hermes_key,
            extra_headers={"X-Hermes-Session-Id": ctx.room.name},
        ),
        tts=openai.TTS(voice="nova"),
        mcp_servers=mcp_servers,
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC()
        ),
    )

    await session.generate_reply(instructions=SESSION_INSTRUCTION)


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
