"""Choose and construct a speech-to-text engine."""

from __future__ import annotations

from ..config import Config
from .base import STTEngine, STTError
from .gemini_stt import GeminiSTT
from .groq_whisper import GroqWhisperSTT
from .local_whisper import LocalWhisperSTT


def make_stt(config: Config, provider_id: str | None = None) -> STTEngine:
    provider_id = (provider_id or config.get("stt.provider", "auto")).lower()
    groq = GroqWhisperSTT(
        api_key=config.get("llm.groq.api_key", ""),
        base_url=config.get("llm.groq.base_url", "https://api.groq.com/openai/v1"),
        model=config.get("stt.groq_model", "whisper-large-v3-turbo"),
    )
    gemini = GeminiSTT(
        api_key=config.get("llm.gemini.api_key", ""),
        base_url=config.get("llm.gemini.base_url", "https://generativelanguage.googleapis.com/v1beta"),
        model=config.get("stt.gemini_model", "gemini-2.5-flash"),
    )
    local = LocalWhisperSTT(config.get("stt.local_model", "base"))

    if provider_id == "groq":
        return groq
    if provider_id == "gemini":
        return gemini
    if provider_id == "local":
        return local
    if provider_id != "auto":
        raise STTError(f"unknown speech-to-text provider: {provider_id}")

    if config.get("llm.groq.api_key"):
        return groq
    if config.get("llm.gemini.api_key"):
        return gemini
    if local.available():
        return local
    # Return Groq so the eventual error tells the user exactly what to add.
    return groq
