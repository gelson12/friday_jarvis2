import httpx


class HermesAdapter:

    def __init__(self, base_url="http://localhost:8080"):
        self.base_url = base_url

    async def chat(
        self,
        session_id: str,
        text: str,
    ):

        async with httpx.AsyncClient(timeout=60) as client:

            response = await client.post(
                f"{self.base_url}/chat",
                json={
                    "session_id": session_id,
                    "message": text,
                    "platform": "livekit"
                }
            )

            response.raise_for_status()

            data = response.json()

            return data["response"]
