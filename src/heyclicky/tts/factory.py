"""Build the configured text-to-speech engine."""

from __future__ import annotations

import shutil

from ..config import Config
from .base import TTSEngine, TTSError
from .edge_tts_engine import EdgeTTSEngine
from .gemini_tts_engine import GeminiTTSEngine
from .offline_tts_engine import PiperTTSEngine, SpeechDispatcherTTSEngine


def make_tts(config: Config, provider_id: str | None = None) -> TTSEngine:
    provider_id = (provider_id or config.get("tts.provider", "auto")).lower()
    volume = float(config.get("tts.volume", 0.9))

    edge = EdgeTTSEngine(voice=config.get("tts.voice", "en-US-AriaNeural"), volume=volume)
    gemini = GeminiTTSEngine(
        api_key=config.get("llm.gemini.api_key", ""),
        base_url=config.get("llm.gemini.base_url", "https://generativelanguage.googleapis.com/v1beta"),
        model=config.get("tts.gemini_model", "gemini-2.5-flash-preview-tts"),
        voice=config.get("tts.gemini_voice", "Kore"),
        volume=volume,
    )
    piper = PiperTTSEngine(model_path=config.get("tts.piper_model", ""), volume=volume)
    system = SpeechDispatcherTTSEngine(volume=volume)

    if provider_id == "edge":
        return edge
    if provider_id == "gemini":
        return gemini
    if provider_id == "piper":
        return piper
    if provider_id in ("speech-dispatcher", "system"):
        return system
    if provider_id != "auto":
        raise TTSError(f"unknown text-to-speech provider: {provider_id}")

    if edge.available():
        return edge
    if config.get("llm.gemini.api_key"):
        return gemini
    if shutil.which("piper") and config.get("tts.piper_model"):
        return piper
    return system


def tts_available(config: Config) -> tuple[bool, str]:
    engine = make_tts(config)
    name = engine.display_name
    if engine.id == "edge":
        return engine.available(), f"{name} (network voice)"
    if engine.id == "gemini":
        return bool(config.get("llm.gemini.api_key")), name
    if engine.id == "piper":
        return bool(shutil.which("piper")) and bool(config.get("tts.piper_model")), name
    return bool(shutil.which("spd-say")), name
