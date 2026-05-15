"""
Groq Speech-to-Text Adapter for LiveKit Agents
PRIMARY: Groq Whisper (unlimited quota, fastest)
FALLBACK: OpenAI, Deepgram, Google Cloud
"""

import os
import logging
from typing import Optional
from groq import Groq
from livekit.agents.stt import SpeechRecognizer, STTCapabilities, Recognition

logger = logging.getLogger(__name__)


class GroqSTT(SpeechRecognizer):
    """Groq Whisper STT with fallback chain"""

    def __init__(self):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        if not self.groq_api_key:
            raise ValueError("GROQ_API_KEY environment variable is required")

        self.client = Groq(api_key=self.groq_api_key)
        self.current_provider = "groq"
        logger.info("✓ Groq STT initialized (unlimited quota)")

    @property
    def capabilities(self) -> STTCapabilities:
        return STTCapabilities(
            streaming=False,
            language=True,
        )

    async def recognize(
        self,
        buffer,
        language: Optional[str] = None,
    ) -> Optional[Recognition]:
        """Transcribe audio using Groq Whisper (no quota limits)"""
        try:
            import io

            if isinstance(buffer, bytes):
                audio_file = io.BytesIO(buffer)
            else:
                audio_file = buffer

            transcript = self.client.audio.transcriptions.create(
                file=("audio.wav", audio_file, "audio/wav"),
                model="whisper-large-v3-turbo",
                language=language or "en",
            )

            self.current_provider = "groq"
            logger.debug(f"✓ Groq transcribed: {transcript.text[:50]}...")

            return Recognition(
                text=transcript.text,
                confidence=1.0,  # Groq doesn't provide confidence scores
            )

        except Exception as e:
            logger.warning(f"⚠ Groq STT failed: {e}")
            logger.info("Falling back to OpenAI STT...")
            return await self._fallback_to_openai(buffer, language)

    async def _fallback_to_openai(
        self,
        buffer,
        language: Optional[str] = None,
    ) -> Optional[Recognition]:
        """Fallback to OpenAI Whisper if Groq fails"""
        try:
            from livekit.plugins import openai

            openai_stt = openai.STT()
            self.current_provider = "openai"
            logger.info("✓ Switched to OpenAI STT")
            return await openai_stt.recognize(buffer, language)

        except Exception as openai_error:
            logger.error(f"✗ OpenAI fallback failed: {openai_error}")
            logger.info("Attempting Deepgram fallback...")
            return await self._fallback_to_deepgram(buffer, language)

    async def _fallback_to_deepgram(
        self,
        buffer,
        language: Optional[str] = None,
    ) -> Optional[Recognition]:
        """Deepgram fallback"""
        try:
            from livekit.plugins import deepgram

            deepgram_stt = deepgram.STT()
            self.current_provider = "deepgram"
            logger.info("✓ Switched to Deepgram STT")
            return await deepgram_stt.recognize(buffer, language)

        except Exception as dg_error:
            logger.error(f"✗ Deepgram fallback failed: {dg_error}")
            logger.info("Attempting Google Cloud fallback...")
            return await self._fallback_to_google(buffer, language)

    async def _fallback_to_google(
        self,
        buffer,
        language: Optional[str] = None,
    ) -> Optional[Recognition]:
        """Google Cloud fallback"""
        try:
            from livekit.plugins import google

            google_stt = google.STT()
            self.current_provider = "google"
            logger.info("✓ Switched to Google Cloud STT")
            return await google_stt.recognize(buffer, language)

        except Exception as google_error:
            logger.error(f"✗ All STT providers exhausted")
            logger.error(f"  Groq error: {e}")
            logger.error(f"  OpenAI error: {openai_error}")
            logger.error(f"  Deepgram error: {dg_error}")
            logger.error(f"  Google error: {google_error}")
            raise RuntimeError("All STT providers failed. Check API keys and quotas.")
