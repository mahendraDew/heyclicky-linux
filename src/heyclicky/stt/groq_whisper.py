"""Groq Whisper transcription (fast, cheap, needs GROQ_API_KEY)."""

from __future__ import annotations

import httpx

from .base import STTEngine, STTError


class GroqWhisperSTT(STTEngine):
    id = "groq"
    display_name = "Groq Whisper"

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no GROQ_API_KEY configured"
        return True, f"Groq Whisper ready ({self.model})"

    def transcribe(self, wav_bytes: bytes, keyterms: list[str], language: str | None = None) -> str:
        if not self.api_key:
            raise STTError("Groq transcription needs an API key. Add one in Settings.")
        if len(wav_bytes) <= 44:
            return ""
        data = {
            "model": self.model,
            "response_format": "json",
            "temperature": "0",
        }
        if language:
            data["language"] = language
        if keyterms:
            # Whisper accepts a free-form prompt; a comma list biases spelling
            # of product names exactly like the original app's keyterms.
            data["prompt"] = ", ".join(keyterms[:40])
        try:
            response = httpx.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data=data,
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise STTError(f"Groq transcription failed: {exc}") from exc
        if response.status_code >= 400:
            raise STTError(
                f"Groq transcription error ({response.status_code}): {response.text[:400]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise STTError("Groq returned an invalid transcription response") from exc
        return self.clean(str(payload.get("text", "")))
