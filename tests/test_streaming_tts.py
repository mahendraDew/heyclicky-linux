from __future__ import annotations

import threading
import time

from heyclicky.tts.base import TTSEngine
from heyclicky.tts.streaming import SentenceSpeaker


class RecordingEngine(TTSEngine):
    id = "fake"
    display_name = "Fake"

    def __init__(self) -> None:
        super().__init__(volume=0.0)
        self.spoken: list[str] = []
        self.release = threading.Event()

    def health(self) -> tuple[bool, str]:
        return True, "ok"

    def _speak(self, text: str, cancel: threading.Event) -> bool:
        self.spoken.append(text)
        return True


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_speaks_by_sentence_while_streaming():
    engine = RecordingEngine()
    speaker = SentenceSpeaker(engine, cancel=threading.Event(), min_chars=5)
    speaker.start()
    speaker.feed("the button is ")
    time.sleep(0.05)
    assert engine.spoken == []  # no complete sentence yet
    speaker.feed("right here. click it")
    assert _wait_for(lambda: engine.spoken == ["the button is right here."])
    speaker.feed(" and you are done.")
    speaker.finish()
    speaker.join(timeout=2.0)
    assert engine.spoken == ["the button is right here.", "click it and you are done."]


def test_point_tag_is_never_spoken():
    engine = RecordingEngine()
    speaker = SentenceSpeaker(engine, cancel=threading.Event(), min_chars=5)
    speaker.start()
    speaker.feed("look at the top left. [POINT:12,3:button]")
    speaker.finish()
    speaker.join(timeout=2.0)
    assert engine.spoken == ["look at the top left."]


def test_incomplete_point_tag_is_held_back():
    engine = RecordingEngine()
    speaker = SentenceSpeaker(engine, cancel=threading.Event(), min_chars=5)
    speaker.start()
    speaker.feed("that is the save button. [POINT:12,3:save")
    time.sleep(0.05)
    assert engine.spoken == ["that is the save button."]
    speaker.feed(" button]")
    speaker.finish()
    speaker.join(timeout=2.0)
    assert engine.spoken == ["that is the save button."]


def test_short_fragments_are_merged():
    engine = RecordingEngine()
    speaker = SentenceSpeaker(engine, cancel=threading.Event(), min_chars=18)
    speaker.start()
    speaker.feed("yes. ")
    time.sleep(0.05)
    assert engine.spoken == []
    speaker.feed("it is on the left.")
    speaker.finish()
    speaker.join(timeout=2.0)
    assert engine.spoken == ["yes. it is on the left."]


def test_cancel_prevents_speech():
    engine = RecordingEngine()
    cancel = threading.Event()
    speaker = SentenceSpeaker(engine, cancel=cancel, min_chars=5)
    speaker.start()
    cancel.set()
    speaker.feed("this should not be spoken.")
    speaker.finish()
    speaker.join(timeout=2.0)
    assert engine.spoken == []
