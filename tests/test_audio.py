from __future__ import annotations

import io
import wave

import numpy as np

from heyclicky.audio import (
    SAMPLE_RATE,
    _fade_in,
    normalize_pcm,
    pcm_to_wav,
    wav_duration_seconds,
)


def _pcm(amplitude: int, seconds: float = 0.5) -> bytes:
    t = np.arange(int(SAMPLE_RATE * seconds), dtype=np.float32) / SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype("<i2").tobytes()


def test_normalize_pcm_lifts_quiet_speech():
    quiet = _pcm(300)
    boosted = normalize_pcm(quiet)
    before = np.frombuffer(quiet, dtype="<i2").astype(np.float32)
    after = np.frombuffer(boosted, dtype="<i2").astype(np.float32)
    assert np.sqrt(np.mean(after**2)) > np.sqrt(np.mean(before**2)) * 3
    assert np.max(np.abs(after)) <= 32767


def test_normalize_pcm_leaves_loud_audio_alone():
    loud = _pcm(20000)
    assert normalize_pcm(loud) == loud


def test_normalize_pcm_leaves_silence_alone():
    silence = b"\x00\x00" * 1000
    assert normalize_pcm(silence) == silence


def test_fade_in_softens_the_first_samples_without_changing_length():
    pcm = (np.ones(SAMPLE_RATE // 5, dtype="<i2") * 10000).tobytes()
    faded = _fade_in(pcm)
    assert len(faded) == len(pcm)
    before = np.frombuffer(pcm, dtype="<i2")
    after = np.frombuffer(faded, dtype="<i2")
    assert abs(int(after[0])) < abs(int(before[0]))
    # Everything past the ramp is untouched.
    assert np.array_equal(after[-1000:], before[-1000:])



def test_pcm_to_wav_roundtrip():
    pcm = b"\x00\x00" * SAMPLE_RATE  # one second of silence
    wav = pcm_to_wav(pcm)
    with wave.open(io.BytesIO(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnframes() == SAMPLE_RATE
    assert wav_duration_seconds(wav) == 1.0


def test_duration_of_bad_wav_is_zero():
    assert wav_duration_seconds(b"not a wav") == 0.0
