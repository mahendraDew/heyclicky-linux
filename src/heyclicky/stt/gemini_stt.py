"""Gemini audio transcription fallback.

Gemini accepts audio inline, so the same key used for vision chat can also
transcribe push-to-talk recordings. This is slower than Groq Whisper but keeps
the app working with a single provider.
"""

from __future__ import annotations

import base64

import httpx

from .base import STTEngine, STTError

PROMPT = """Transcribe the user's speech exactly as spoken. Return only the transcript, with no preamble, quotes, labels or commentary. Preserve technical terms and product names. If there is no speech, return an empty string."""


class GeminiSTT(STTEngine):
    id = "gemini"
    display_name = "Gemini audio"

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no GEMINI_API_KEY configured"
        return True, f"Gemini audio ready ({self.model})"

    def transcribe(self, wav_bytes: bytes, keyterms: list[str], language: str | None = None) -> str:
        if not self.api_key:
            raise STTError("Gemini transcription needs an API key. Add one in Settings.")
        if len(wav_bytes) <= 44:
            return ""
        hint = ""
        if keyterms:
            hint = "\nTerms that may appear: " + ", ".join(keyterms[:40])
        if language:
            hint += f"\nLanguage hint: {language}"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": base64.b64encode(wav_bytes).decode("ascii"),
                            }
                        },
                        {"text": PROMPT + hint},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024},
        }
        try:
            response = httpx.post(
                f"{self.base_url}/models/{self.model}:generateContent",
                params={"key": self.api_key},
                json=body,
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise STTError(f"Gemini transcription failed: {exc}") from exc
        if response.status_code >= 400:
            raise STTError(
                f"Gemini transcription error ({response.status_code}): {response.text[:400]}"
            )
        try:
            payload = response.json()
            candidates = payload.get("candidates") or []
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(part.get("text", "") for part in parts)
        except (ValueError, IndexError) as exc:
            raise STTError("Gemini returned an invalid transcription response") from exc
        return self.clean(text)
