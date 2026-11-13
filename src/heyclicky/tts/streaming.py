"""Speak a streamed LLM reply sentence by sentence.

Synthesising the whole answer before the first sound plays adds a full
text-to-speech round trip after the model finishes. Instead, this helper is fed
text deltas as they arrive and starts speaking as soon as it has one complete
sentence. Pointing tags (``[POINT:x,y:label]``) are never spoken.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Callable

from .base import TTSEngine

log = logging.getLogger(__name__)

# A sentence ends at punctuation followed by whitespace, or at a newline.
_SENTENCE_END = re.compile(r"[.!?;:](?=\s)|\n")
_POINT_TAG = re.compile(r"\[POINT:[^\]]*\]", re.IGNORECASE)


def _split_point_tag(text: str) -> tuple[str, str]:
    """Return (speakable text, held-back incomplete tag)."""
    clean = _POINT_TAG.sub("", text)
    bracket = clean.find("[")
    if bracket == -1:
        return clean, ""
    return clean[:bracket], clean[bracket:]


class SentenceSpeaker:
    """Consume streamed text and speak it in order on a worker thread."""

    def __init__(
        self,
        engine: TTSEngine,
        *,
        cancel: threading.Event,
        on_start: Callable[[], None] | None = None,
        min_chars: int = 18,
    ) -> None:
        self.engine = engine
        self.cancel = cancel
        self.on_start = on_start
        self.min_chars = min_chars
        self._buffer = ""
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="heyclicky-speech", daemon=True
        )
        self._started_speaking = False
        self._lock = threading.Lock()

    # -- public API -----------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def feed(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._buffer += text
            self._drain(final=False)

    def finish(self) -> None:
        with self._lock:
            self._drain(final=True)
        self._queue.put(None)

    def stop(self) -> None:
        """Cancel pending speech and interrupt playback."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put(None)
        try:
            self.engine.stop()
        except Exception:
            log.debug("tts stop failed", exc_info=True)

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # -- internals ------------------------------------------------------
    def _enqueue(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self._queue.put(sentence)

    def _drain(self, *, final: bool) -> None:
        speakable, held = _split_point_tag(self._buffer)
        text = speakable
        while True:
            match = _SENTENCE_END.search(text)
            if match is None:
                break
            sentence = text[: match.end()].strip()
            rest = text[match.end() :].lstrip()
            if len(sentence) < self.min_chars:
                # Too short to sound natural on its own: merge it with the
                # next completed sentence ("yes." + "it is on the left.").
                following = _SENTENCE_END.search(rest)
                if following is None:
                    break
                sentence = f"{sentence} {rest[: following.end()].strip()}".strip()
                rest = rest[following.end() :].lstrip()
                if len(sentence) < self.min_chars:
                    break
            self._enqueue(sentence)
            text = rest
        if final:
            self._enqueue(text)
            text = ""
        self._buffer = text + held

    def _run(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:
                return
            if self.cancel.is_set():
                continue
            try:
                if not self._started_speaking:
                    self._started_speaking = True
                    if self.on_start is not None:
                        self.on_start()
                self.engine.speak(sentence, cancel=self.cancel)
            except Exception as exc:  # speech is a nicety, never fatal
                log.warning("TTS failed: %s", exc)
