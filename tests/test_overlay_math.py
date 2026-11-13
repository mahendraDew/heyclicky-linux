from __future__ import annotations

import math

from heyclicky.state import VoiceState
from heyclicky.ui.overlay import glow_intensity


def test_glow_is_off_when_idle():
    assert glow_intensity(VoiceState.IDLE, 0.5, 1.0) == 0.0


def test_glow_reacts_to_audio_level():
    quiet = glow_intensity(VoiceState.LISTENING, 0.0, 0.0)
    loud = glow_intensity(VoiceState.LISTENING, 0.9, 0.0)
    assert 0.0 < quiet < loud <= 1.0


def test_glow_pulses_with_phase():
    values = {
        glow_intensity(VoiceState.LISTENING, 0.0, phase)
        for phase in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2)
    }
    assert len(values) > 1


def test_glow_is_clamped_for_any_input():
    for state in (VoiceState.LISTENING, VoiceState.PROCESSING, VoiceState.RESPONDING):
        for level in (-5.0, 0.0, 0.5, 5.0):
            value = glow_intensity(state, level, 2.0)
            assert 0.0 <= value <= 1.0
            assert math.isfinite(value)


def test_processing_and_responding_are_dimmer_than_loud_listening():
    assert glow_intensity(VoiceState.PROCESSING, 0.0, 0.0) < glow_intensity(
        VoiceState.LISTENING, 1.0, 0.0
    )
    assert glow_intensity(VoiceState.RESPONDING, 0.0, 0.0) <= glow_intensity(
        VoiceState.PROCESSING, 0.0, 0.0
    )
