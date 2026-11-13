from __future__ import annotations

import copy
import io
import time

import pytest
from PIL import Image

from heyclicky.capture import CaptureBundle, CaptureImage, ScreenInfo
from heyclicky.config import DEFAULT_CONFIG, Config
from heyclicky.hotkey import ManualHotkeyBackend
from heyclicky.llm.base import ProviderError
from heyclicky.pipeline import Pipeline
from heyclicky.state import VoiceState


class FakeOverlay:
    def __init__(self) -> None:
        self.visible = True
        self.states: list[VoiceState] = []
        self.points = []
        self.point_finished_cb = None

    def rebuild(self):
        pass

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def destroy(self):
        self.visible = False

    def welcome(self):
        pass

    def prompt(self, text):
        pass

    def clear_point(self):
        pass

    def hide_prompt(self):
        pass

    def cancel_navigation(self):
        pass

    def set_state(self, state):
        self.states.append(state)

    def set_audio_level(self, level):
        pass

    def hide_for_capture(self):
        pass

    def show_after_capture(self):
        pass

    def fade_out_and_hide(self):
        self.visible = False

    def active_navigation(self):
        return False

    def point_at(self, target, screens):
        self.points.append(target)

    def set_point_finished_callback(self, callback):
        self.point_finished_cb = callback


class FakeSTT:
    id = "fake"
    display_name = "Fake STT"

    def __init__(self, transcript="how do i search"):
        self.transcript = transcript

    def transcribe(self, wav, keyterms, language=None):
        return self.transcript

    def health(self):
        return True, "fake"


class FakeTTS:
    id = "fake"
    display_name = "Fake TTS"

    def __init__(self):
        self.spoken: list[str] = []
        self.volume = 1.0
        self.stopped = False

    def speak(self, text, cancel=None):
        self.spoken.append(text)
        return True

    def stop(self):
        self.stopped = True

    @property
    def is_playing(self):
        return False


class FakeProvider:
    id = "fake"
    display_name = "Fake"
    supports_vision = True

    def __init__(self, response="look at the search bar [POINT:100,50:search bar]", error=None):
        self.model = "fake-vision"
        self.response = response
        self.error = error
        self.requests = []

    def health(self):
        return True, "fake"

    def list_models(self):
        return [self.model]

    def stream_chat(self, request, on_delta=None, cancel=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        if on_delta:
            on_delta(self.response)
        return self.response


class ChunkedProvider(FakeProvider):
    """Streams multiple deltas so the chat view and TTS can start early."""

    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks

    def stream_chat(self, request, on_delta=None, cancel=None):
        self.requests.append(request)
        for chunk in self.chunks:
            if on_delta:
                on_delta(chunk)
        return "".join(self.chunks)


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _bundle(two_screens: bool = False) -> CaptureBundle:
    primary = ScreenInfo("DP-1", 0, 0, 1920, 1080)
    images = [
        CaptureImage(
            screen=primary,
            data=_jpeg(),
            pixel_width=1280,
            pixel_height=720,
            label="screen 1",
            is_cursor_screen=True,
        )
    ]
    if two_screens:
        secondary = ScreenInfo("DP-2", 1920, 0, 1920, 1080)
        images.append(
            CaptureImage(
                screen=secondary,
                data=_jpeg(),
                pixel_width=1280,
                pixel_height=720,
                label="screen 2",
                is_cursor_screen=False,
            )
        )
    return CaptureBundle(images=images, backend="fake", captured_at=time.time())


class FakeCapture:
    def __init__(self, two_screens: bool = False):
        self.two_screens = two_screens
        self.last_error = None
        self.calls = 0

    def screens_provider(self):
        screens = [ScreenInfo("DP-1", 0, 0, 1920, 1080)]
        if self.two_screens:
            screens.append(ScreenInfo("DP-2", 1920, 0, 1920, 1080))
        return screens

    def cursor_provider(self):
        return (500.0, 500.0)

    def capture_all(self, screens=None, cursor=None):
        self.calls += 1
        return _bundle(self.two_screens)

    def backend_name(self):
        return "Fake capture"


def _config(show_cursor: bool = True) -> Config:
    config = Config(data=copy.deepcopy(DEFAULT_CONFIG))
    config.set("ui.edge_glow", show_cursor)
    config.set("tts.enabled", True)
    return config


def _make_pipeline(qtbot, *, response=None, error=None, two_screens=False, transcript="how do i search"):
    config = _config()
    overlay = FakeOverlay()
    stt = FakeSTT(transcript)
    tts = FakeTTS()
    provider = FakeProvider(response=response or "look at the search bar [POINT:100,50:search bar]", error=error)
    capture = FakeCapture(two_screens=two_screens)
    pipeline = Pipeline(
        config,
        overlay,  # type: ignore[arg-type]
        stt_factory=lambda _config: stt,
        tts_factory=lambda _config: tts,
        provider_factory=lambda _config, _provider: provider,
        capture_service=capture,  # type: ignore[arg-type]
        hotkey=ManualHotkeyBackend(),
        welcome_on_start=False,
    )
    pipeline.start()
    return pipeline, overlay, stt, tts, provider, capture


def test_response_streams_reply_points_and_speaks(qtbot):
    pipeline, overlay, _stt, tts, provider, capture = _make_pipeline(qtbot)
    try:
        with qtbot.waitSignals([pipeline.reply_ready, pipeline.point_requested], timeout=3000):
            pipeline._begin_response(b"wav", [ScreenInfo("DP-1", 0, 0, 1920, 1080)], (500.0, 500.0))
        qtbot.waitUntil(lambda: bool(tts.spoken), timeout=3000)
        qtbot.waitUntil(lambda: pipeline.state is VoiceState.IDLE, timeout=3000)

        assert pipeline.last_transcript == "how do i search"
        assert pipeline.last_reply == "look at the search bar"
        assert len(pipeline.history.exchanges) == 1
        assert pipeline.history.exchanges[0][0] == "how do i search"
        assert pipeline.history.exchanges[0][1] == "look at the search bar"
        assert tts.spoken == ["look at the search bar"]
        assert len(overlay.points) == 1
        target = overlay.points[0]
        assert target.global_x == pytest.approx(150.0)
        assert target.global_y == pytest.approx(75.0)
        assert target.label == "search bar"
        assert capture.calls == 1
        # The point tag is stripped before it reaches history/TTS.
        assert "POINT" not in tts.spoken[0]
    finally:
        pipeline.stop()


def test_reply_deltas_reach_the_panel_and_speech_starts_per_sentence(qtbot):
    config = _config()
    overlay = FakeOverlay()
    stt = FakeSTT("where is search")
    tts = FakeTTS()
    provider = ChunkedProvider(
        [
            "it's in the top bar. ",
            "click the magnifier. ",
            "[POINT:100,50:search bar]",
        ]
    )
    pipeline = Pipeline(
        config,
        overlay,  # type: ignore[arg-type]
        stt_factory=lambda _config: stt,
        tts_factory=lambda _config: tts,
        provider_factory=lambda _config, _provider: provider,
        capture_service=FakeCapture(),  # type: ignore[arg-type]
        hotkey=ManualHotkeyBackend(),
        welcome_on_start=False,
    )
    deltas: list[str] = []
    pipeline.reply_delta.connect(deltas.append)
    pipeline.start()
    try:
        with qtbot.waitSignal(pipeline.reply_ready, timeout=3000):
            pipeline._begin_response(b"wav", [ScreenInfo("DP-1", 0, 0, 1920, 1080)], (500.0, 500.0))
        qtbot.waitUntil(lambda: pipeline.state is VoiceState.IDLE, timeout=3000)
        assert "".join(deltas) == "".join(provider.chunks)
        assert tts.spoken == ["it's in the top bar.", "click the magnifier."]
        assert pipeline.last_reply == "it's in the top bar. click the magnifier."
    finally:
        pipeline.stop()


def test_point_none_does_not_fly(qtbot):
    pipeline, overlay, _stt, _tts, _provider, _capture = _make_pipeline(
        qtbot, response="that's a general question [POINT:none]"
    )
    try:
        with qtbot.waitSignal(pipeline.reply_ready, timeout=3000):
            pipeline._begin_response(b"wav", [ScreenInfo("DP-1", 0, 0, 1920, 1080)], (500.0, 500.0))
        qtbot.waitUntil(lambda: pipeline.state is VoiceState.IDLE, timeout=3000)
        assert overlay.points == []
        assert pipeline.last_reply == "that's a general question"
    finally:
        pipeline.stop()


def test_second_screen_pointing_uses_screen_offset(qtbot):
    pipeline, overlay, _stt, _tts, _provider, _capture = _make_pipeline(
        qtbot,
        response="it's on your other monitor [POINT:100,50:terminal:screen2]",
        two_screens=True,
    )
    try:
        with qtbot.waitSignal(pipeline.point_requested, timeout=3000):
            pipeline._begin_response(
                b"wav",
                [ScreenInfo("DP-1", 0, 0, 1920, 1080), ScreenInfo("DP-2", 1920, 0, 1920, 1080)],
                (500.0, 500.0),
            )
        qtbot.waitUntil(lambda: pipeline.state is VoiceState.IDLE, timeout=3000)
        target = overlay.points[0]
        assert target.screen_number == 2
        assert target.global_x == pytest.approx(1920 + 150.0)
        assert target.global_y == pytest.approx(75.0)
    finally:
        pipeline.stop()


def test_provider_error_is_reported_and_spoken(qtbot):
    pipeline, _overlay, _stt, tts, _provider, _capture = _make_pipeline(
        qtbot, error=ProviderError("Gemini needs an API key")
    )
    try:
        with qtbot.waitSignal(pipeline.error, timeout=3000) as blocker:
            pipeline._begin_response(b"wav", [ScreenInfo("DP-1", 0, 0, 1920, 1080)], (0.0, 0.0))
        qtbot.waitUntil(lambda: pipeline.state is VoiceState.IDLE, timeout=3000)
        assert "API key" in blocker.args[0]
        assert tts.spoken  # friendly fallback spoken aloud
    finally:
        pipeline.stop()


def test_empty_transcript_does_not_capture(qtbot):
    pipeline, _overlay, _stt, _tts, _provider, capture = _make_pipeline(qtbot, transcript="")
    try:
        with qtbot.waitSignal(pipeline.error, timeout=3000) as blocker:
            pipeline._begin_response(b"wav", [ScreenInfo("DP-1", 0, 0, 1920, 1080)], (0.0, 0.0))
        qtbot.waitUntil(lambda: pipeline.state is VoiceState.IDLE, timeout=3000)
        assert "didn't catch" in blocker.args[0]
        assert capture.calls == 0
    finally:
        pipeline.stop()


def test_cancel_stops_tts_and_ignores_stale_worker():
    config = _config()
    overlay = FakeOverlay()
    tts = FakeTTS()
    pipeline = Pipeline(
        config,
        overlay,  # type: ignore[arg-type]
        stt_factory=lambda _config: FakeSTT(),
        tts_factory=lambda _config: tts,
        provider_factory=lambda _config, _provider: FakeProvider(),
        capture_service=FakeCapture(),  # type: ignore[arg-type]
        hotkey=ManualHotkeyBackend(),
        welcome_on_start=False,
    )
    pipeline.start()
    generation = pipeline._generation
    pipeline.cancel()
    assert pipeline._generation == generation + 1
    assert tts.stopped
    assert pipeline._cancel.is_set()
    assert not pipeline._is_current(generation)
    pipeline.stop()
