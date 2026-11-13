"""Optional fully-local transcription via faster-whisper.

This backend is only used when the user explicitly selects it or when no cloud
speech key exists and ``faster-whisper`` is installed. Keeping it optional keeps
the default install small.
"""

from __future__ import annotations

import os
import tempfile
import threading

from .base import STTEngine, STTError


class LocalWhisperSTT(STTEngine):
    id = "local"
    display_name = "Local Whisper"

    _model_lock = threading.Lock()
    _models: dict[str, object] = {}

    def __init__(self, model_size: str = "base") -> None:
        self.model_size = model_size

    @staticmethod
    def available() -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def health(self) -> tuple[bool, str]:
        if not self.available():
            return False, "faster-whisper is not installed (pip install faster-whisper)"
        return True, f"Local Whisper ready ({self.model_size})"

    def _load_model(self):
        if not self.available():
            raise STTError(
                "Local transcription needs faster-whisper. Install it with "
                "`pip install faster-whisper` or choose Groq/Gemini in Settings."
            )
        with self._model_lock:
            model = self._models.get(self.model_size)
            if model is None:
                from faster_whisper import WhisperModel

                model = WhisperModel(self.model_size, device="auto", compute_type="auto")
                self._models[self.model_size] = model
            return model

    def transcribe(self, wav_bytes: bytes, keyterms: list[str], language: str | None = None) -> str:
        if len(wav_bytes) <= 44:
            return ""
        model = self._load_model()
        # faster-whisper wants a path or file-like object with a name it can
        # decode; a named temp file is the most portable option.
        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)  # noqa: SIM115
        try:
            handle.write(wav_bytes)
            handle.flush()
            handle.close()
            segments, _info = model.transcribe(
                handle.name,
                language=language,
                initial_prompt=", ".join(keyterms[:40]) if keyterms else None,
                vad_filter=True,
                beam_size=5,
            )
            text = " ".join(segment.text.strip() for segment in segments)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        return self.clean(text)
