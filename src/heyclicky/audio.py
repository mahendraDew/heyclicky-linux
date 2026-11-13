"""Microphone capture for push-to-talk on Linux.

Uses ``arecord`` (ALSA) or ``parec`` (PulseAudio/PipeWire) instead of a native
library so the app works on a stock Ubuntu install. Raw S16_LE mono samples are
buffered in memory, metered for the waveform, and wrapped in a WAV header only
when the user releases the shortcut.
"""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
# 50 ms of audio at 16 kHz mono S16 -> 1600 bytes.
READ_CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS // 20

# Quiet laptop mics are the most common cause of "the app does not hear me".
# PulseAudio can leave the input at 15-20 % (-40 dB), which Whisper treats as
# silence, so lift quiet recordings to a healthy level before uploading.
STT_TARGET_RMS = 0.08
STT_MAX_GAIN = 20.0
STT_SILENCE_RMS = 0.0008

# Opening an ALSA capture stream produces a loud pop for the first ~100 ms on
# many laptops. It dwarfs the actual speech and makes Whisper hallucinate, so
# drop that warm-up window before buffering anything.
WARMUP_SECONDS = 0.09
# A 10 ms fade-in hides any residual click at the very first kept sample.
FADE_IN_SECONDS = 0.01


class AudioError(RuntimeError):
    pass


@dataclass
class AudioDevice:
    id: str
    description: str


def list_input_devices() -> list[AudioDevice]:
    devices = [AudioDevice("default", "System default input")]
    if shutil.which("arecord"):
        try:
            output = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, timeout=4
            ).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        for line in output.splitlines():
            if not line.startswith("card "):
                continue
            # card 1: Generic_1 [HD-Audio Generic], device 0: ALC257 Analog [...]
            try:
                card_part, device_part = line.split(", device ")
                card_number = card_part.split(":")[0].split()[1]
                device_number = device_part.split(":")[0].strip()
                description = line.strip()
                device_id = f"plughw:{card_number},{device_number}"
                devices.append(AudioDevice(device_id, description))
            except (IndexError, ValueError):
                continue
    return devices


class MicrophoneRecorder:
    """Records until :meth:`stop`, with live RMS metering."""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        device: str = "default",
        max_seconds: float = 120.0,
        on_level: Callable[[float], None] | None = None,
        on_max_reached: Callable[[], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.max_seconds = max_seconds
        self.on_level = on_level
        self.on_max_reached = on_max_reached

        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._level = 0.0
        self._max_notified = False
        self._command: list[str] = []

    # -- lifecycle ------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def current_level(self) -> float:
        with self._lock:
            return self._level

    def start(self) -> None:
        if self.is_recording:
            return
        with self._lock:
            self._buffer.clear()
            self._level = 0.0
        self._stop_event.clear()
        self._max_notified = False
        self._command = self._build_command()
        try:
            self._process = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise AudioError(f"could not start microphone capture: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="heyclicky-mic", daemon=True)
        self._reader.start()

    def stop(self) -> bytes:
        """Stop recording and return a complete mono 16-bit WAV payload."""
        process = self._process
        self._stop_event.set()
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        self._process = None
        with self._lock:
            pcm = bytes(self._buffer)
            self._buffer.clear()
            self._level = 0.0
        return pcm_to_wav(normalize_pcm(_fade_in(pcm, sample_rate=self.sample_rate)), sample_rate=self.sample_rate)

    def cancel(self) -> None:
        self.stop()

    # -- internals ------------------------------------------------------
    def _build_command(self) -> list[str]:
        if shutil.which("arecord"):
            return [
                "arecord",
                "-q",
                "-D",
                self.device or "default",
                "-f",
                "S16_LE",
                "-r",
                str(self.sample_rate),
                "-c",
                str(CHANNELS),
                "-t",
                "raw",
            ]
        if shutil.which("parec"):
            return [
                "parec",
                "-d",
                self.device or "default",
                "--format=s16le",
                "--rate",
                str(self.sample_rate),
                "--channels",
                str(CHANNELS),
                "--raw",
            ]
        raise AudioError(
            "No microphone capture tool found. Install ALSA tools: sudo apt install alsa-utils"
        )

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        max_bytes = int(self.max_seconds * self.sample_rate * SAMPLE_WIDTH * CHANNELS)
        skip_bytes = int(WARMUP_SECONDS * self.sample_rate * SAMPLE_WIDTH * CHANNELS)
        smoothed = 0.0
        while not self._stop_event.is_set():
            chunk = process.stdout.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            if skip_bytes > 0:
                if len(chunk) <= skip_bytes:
                    skip_bytes -= len(chunk)
                    continue
                chunk = chunk[skip_bytes:]
                skip_bytes = 0
            with self._lock:
                self._buffer.extend(chunk)
                buffered = len(self._buffer)
                level = _rms_level(chunk)
                smoothed = max(level, smoothed * 0.72)
                self._level = smoothed
            if self.on_level is not None:
                try:
                    self.on_level(smoothed)
                except Exception:
                    pass
            if buffered >= max_bytes and not self._max_notified:
                self._max_notified = True
                if self.on_max_reached is not None:
                    try:
                        self.on_max_reached()
                    except Exception:
                        pass
                break


def _rms_level(chunk: bytes) -> float:
    """Same perceptual boost used by BuddyDictationManager.updateAudioPowerLevel."""
    if len(chunk) < 2:
        return 0.0
    sample_count = len(chunk) // 2
    total = 0
    # int.from_bytes in a loop is slow; bytes are little-endian S16.
    for index in range(0, sample_count * 2, 2):
        sample = int.from_bytes(chunk[index : index + 2], "little", signed=True)
        total += sample * sample
    rms = math.sqrt(total / sample_count) / 32768.0
    return min(max(rms * 10.2, 0.0), 1.0)


def _fade_in(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Ramp the first few milliseconds up from silence to hide a stream click."""
    frames = int(FADE_IN_SECONDS * sample_rate)
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    if frames <= 1 or usable < frames * SAMPLE_WIDTH:
        return pcm
    samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)
    samples[:frames] *= np.linspace(0.0, 1.0, frames, dtype=np.float32)
    return samples.astype("<i2").tobytes() + pcm[usable:]


def normalize_pcm(
    pcm: bytes,
    *,
    target_rms: float = STT_TARGET_RMS,
    max_gain: float = STT_MAX_GAIN,
) -> bytes:
    """Lift a quiet recording to ``target_rms`` without ever making it louder.

    Loud recordings are returned untouched (gain is capped at 1.0) and near
    silence is left alone so we do not amplify fan noise into fake speech.
    """
    if len(pcm) < 2:
        return pcm
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return pcm
    scaled = samples / 32768.0
    rms = float(np.sqrt(np.mean(np.square(scaled))))
    if rms <= STT_SILENCE_RMS:
        return pcm
    gain = target_rms / rms
    peak = float(np.max(np.abs(scaled)))
    if peak > 0.0:
        gain = min(gain, 0.98 / peak)
    gain = min(gain, max_gain)
    if gain <= 1.05:
        return pcm
    boosted = np.clip(samples * gain, -32768, 32767).astype("<i2")
    return boosted.tobytes()


def probe_microphone(seconds: float = 1.0) -> tuple[bool, str]:
    """Record briefly and report whether the default input produces signal.

    This catches the classic "it cannot hear me" setup bug: a muted or very
    quiet PulseAudio source. The recording is discarded.
    """
    recorder = MicrophoneRecorder()
    try:
        recorder.start()
    except AudioError as exc:
        return False, str(exc)
    time.sleep(seconds)
    try:
        wav_bytes = recorder.stop()
    except Exception as exc:  # noqa: BLE001 - surfaced to the doctor
        return False, f"could not record from the default input: {exc}"

    pcm = wav_bytes[44:]
    if len(pcm) < SAMPLE_RATE * SAMPLE_WIDTH // 4:
        return False, "the default input produced almost no audio"
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)], dtype="<i2")
    if samples.size == 0:
        return False, "the default input produced no samples"
    peak = int(np.max(np.abs(samples.astype(np.int32))))
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32) / 32768.0))))
    if peak < 120:
        return False, "the microphone is silent — unmute it or raise the input volume"
    if rms < 0.005:
        return True, f"very quiet input (RMS {rms:.3f}); Clicky boosts it automatically"
    return True, f"input level ok (RMS {rms:.3f}, peak {peak})"


def pcm_to_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate() or SAMPLE_RATE
            return frames / float(rate)
    except (wave.Error, EOFError):
        return 0.0
