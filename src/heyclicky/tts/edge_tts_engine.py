"""Free, high-quality TTS through Microsoft Edge's neural voices.

``edge-tts`` needs no API key and works well on Linux. It is the default so a
fresh install can talk back immediately; users who prefer a fully offline setup
can switch to Piper or speech-dispatcher in Settings.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading

from .base import TTSEngine, TTSError


class EdgeTTSEngine(TTSEngine):
    id = "edge"
    display_name = "Edge neural voice"

    def __init__(self, *, voice: str = "en-US-AriaNeural", volume: float = 0.9) -> None:
        super().__init__(volume=volume)
        self.voice = voice

    @staticmethod
    def available() -> bool:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False
        return True

    def _speak(self, text: str, cancel: threading.Event) -> bool:
        if not self.available():
            raise TTSError("edge-tts is not installed (pip install edge-tts)")
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
                path = handle.name

            async def synthesize() -> None:
                import edge_tts

                communicate = edge_tts.Communicate(text, self.voice)
                with open(path, "wb") as audio_file:
                    async for chunk in communicate.stream():
                        if cancel.is_set() or self._stop_event.is_set():
                            return
                        if chunk.get("type") == "audio" and chunk.get("data"):
                            audio_file.write(chunk["data"])

            asyncio.run(synthesize())
            if cancel.is_set() or self._stop_event.is_set():
                return False
            if os.path.getsize(path) == 0:
                raise TTSError("Edge TTS returned no audio")
            return self._play_file(path, cancel)
        except TTSError:
            raise
        except Exception as exc:  # edge-tts raises a variety of network errors
            raise TTSError(f"Edge TTS failed: {exc}") from exc
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
