"""Gemini TTS (optional alternative to Edge voices)."""

from __future__ import annotations

import base64
import os
import tempfile
import threading
import wave

import httpx

from .base import TTSEngine, TTSError


class GeminiTTSEngine(TTSEngine):
    id = "gemini"
    display_name = "Gemini voice"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = "gemini-2.5-flash-preview-tts",
        voice: str = "Kore",
        volume: float = 0.9,
    ) -> None:
        super().__init__(volume=volume)
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice

    def _speak(self, text: str, cancel: threading.Event) -> bool:
        if not self.api_key:
            raise TTSError("Gemini voice needs an API key. Add one in Settings.")
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}
                },
            },
        }
        try:
            response = httpx.post(
                f"{self.base_url}/models/{self.model}:generateContent",
                params={"key": self.api_key},
                json=body,
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"Gemini voice failed: {exc}") from exc
        if response.status_code >= 400:
            raise TTSError(f"Gemini voice error ({response.status_code}): {response.text[:300]}")
        try:
            payload = response.json()
            parts = payload["candidates"][0]["content"]["parts"]
            inline = next(part["inlineData"] for part in parts if "inlineData" in part)
            pcm = base64.b64decode(inline["data"])
            mime = inline.get("mimeType", "audio/L16;rate=24000")
        except (KeyError, IndexError, ValueError, StopIteration) as exc:
            raise TTSError("Gemini voice returned an unexpected response") from exc

        rate = 24000
        for piece in mime.split(";"):
            if piece.strip().startswith("rate="):
                try:
                    rate = int(piece.split("=", 1)[1])
                except ValueError:
                    pass

        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                path = handle.name
            with wave.open(path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(rate)
                wav_file.writeframes(pcm)
            if cancel.is_set() or self._stop_event.is_set():
                return False
            return self._play_file(path, cancel)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
