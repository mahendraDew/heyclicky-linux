"""Offline TTS backends: Piper and speech-dispatcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading

from .base import TTSEngine, TTSError


class PiperTTSEngine(TTSEngine):
    id = "piper"
    display_name = "Piper (offline)"

    def __init__(self, *, model_path: str = "", volume: float = 0.9) -> None:
        super().__init__(volume=volume)
        self.model_path = model_path

    def _speak(self, text: str, cancel: threading.Event) -> bool:
        if not shutil.which("piper"):
            raise TTSError("Piper is not installed. Install it or choose another voice in Settings.")
        if not self.model_path or not os.path.exists(self.model_path):
            raise TTSError("Choose a Piper .onnx voice model in Settings.")
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                path = handle.name
            process = subprocess.Popen(
                ["piper", "--model", self.model_path, "--output_file", path],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                process.communicate(text, timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                raise TTSError("Piper timed out") from None
            if cancel.is_set() or self._stop_event.is_set():
                return False
            if process.returncode != 0 or os.path.getsize(path) == 0:
                raise TTSError("Piper failed to synthesize audio")
            return self._play_file(path, cancel)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


class SpeechDispatcherTTSEngine(TTSEngine):
    """Last-resort offline voice using the desktop's speech-dispatcher."""

    id = "speech-dispatcher"
    display_name = "System voice (spd-say)"

    def __init__(self, *, volume: float = 0.9) -> None:
        super().__init__(volume=volume)
        self._spd_process: subprocess.Popen | None = None

    def _speak(self, text: str, cancel: threading.Event) -> bool:
        if not shutil.which("spd-say"):
            raise TTSError("spd-say is not installed (sudo apt install speech-dispatcher)")
        try:
            process = subprocess.Popen(
                ["spd-say", "-w", "--", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise TTSError(f"could not start spd-say: {exc}") from exc
        self._spd_process = process
        try:
            while process.poll() is None:
                if cancel.is_set() or self._stop_event.is_set():
                    subprocess.run(["spd-say", "-C"], capture_output=True, timeout=3)
                    return False
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            return process.returncode == 0
        finally:
            self._spd_process = None

    def _cancel_backend(self) -> None:
        if shutil.which("spd-say"):
            try:
                subprocess.run(["spd-say", "-C"], capture_output=True, timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
