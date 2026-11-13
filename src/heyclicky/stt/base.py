"""Speech-to-text provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class STTError(RuntimeError):
    """A user-facing transcription failure."""


class STTEngine(ABC):
    id: str = "base"
    display_name: str = "Base"

    @abstractmethod
    def transcribe(self, wav_bytes: bytes, keyterms: list[str], language: str | None = None) -> str:
        """Return the final transcript for a complete push-to-talk recording."""

    @abstractmethod
    def health(self) -> tuple[bool, str]:
        """Return (ok, human readable detail) without recording anything."""

    @staticmethod
    def clean(text: str) -> str:
        text = (text or "").strip()
        # Whisper sometimes emits bracketed non-speech markers or wraps output
        # in quotes. Neither should be spoken or sent to the LLM.
        for marker in ("[BLANK_AUDIO]", "[silence]", "(silence)", "[Music]", "[music]"):
            text = text.replace(marker, " ")
        text = text.strip().strip('"').strip("'").strip()
        return " ".join(text.split())
