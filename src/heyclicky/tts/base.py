"""Text-to-speech provider interface and audio playback helpers."""

from __future__ import annotations

import shutil
import subprocess
import threading
from abc import ABC, abstractmethod


class TTSError(RuntimeError):
    """A user-facing speech synthesis or playback failure."""


class TTSEngine(ABC):
    id: str = "base"
    display_name: str = "Base"

    def __init__(self, *, volume: float = 0.9) -> None:
        self.volume = max(0.0, min(1.0, volume))
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # -- public API -----------------------------------------------------
    def speak(self, text: str, cancel: threading.Event | None = None) -> bool:
        """Speak text. Returns True when playback completed, False if stopped."""
        text = (text or "").strip()
        if not text:
            return True
        self._stop_event.clear()
        return self._speak(text, cancel or threading.Event())

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self._cancel_backend()

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    # -- subclass hooks -------------------------------------------------
    @abstractmethod
    def _speak(self, text: str, cancel: threading.Event) -> bool:
        ...

    def _cancel_backend(self) -> None:  # noqa: B027 - optional hook
        """Optional hook for engines whose playback is not a subprocess."""

    # -- helpers --------------------------------------------------------
    def _play_file(self, path: str, cancel: threading.Event) -> bool:
        """Play an audio file with the first available Linux player."""
        if shutil.which("ffplay"):
            command = [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-volume",
                str(int(self.volume * 100)),
                path,
            ]
        elif shutil.which("paplay"):
            command = ["paplay", path]
        elif shutil.which("aplay"):
            command = ["aplay", "-q", path]
        else:
            raise TTSError(
                "No audio player found. Install ffmpeg or pulseaudio-utils "
                "(sudo apt install ffmpeg)."
            )
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise TTSError(f"could not start audio playback: {exc}") from exc
        with self._lock:
            self._process = process
        try:
            while process.poll() is None:
                if cancel.is_set() or self._stop_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return False
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            return process.returncode == 0
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
