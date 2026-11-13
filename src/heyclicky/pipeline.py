"""The push-to-talk pipeline: record -> transcribe -> capture -> think -> point -> speak.

This is the Linux counterpart of ``CompanionManager.sendTranscriptToClaudeWithScreenshot``.
It owns the voice state machine, the conversation history, cancellation, the
``[POINT:x,y:label]`` flow and the transient overlay hide.

Everything that touches Qt widgets happens on the GUI thread. Network calls,
speech-to-text and text-to-speech run on a short-lived worker thread and report
back through signals, so the overlay keeps animating while the model streams.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .audio import MicrophoneRecorder, wav_duration_seconds
from .capture import CaptureBundle, CaptureError, ScreenCaptureService, ScreenInfo
from .config import Config
from .geometry import choose_target_screen, map_screenshot_point
from .hotkey import HotkeyBackend, HotkeyError, hotkey_label, resolve_hotkey_backend
from .llm.base import ChatRequest, ImageAttachment, LLMProvider, ProviderError, StreamCancelled
from .llm.factory import DISPLAY_NAMES as LLM_DISPLAY_NAMES
from .llm.factory import make_provider
from .pointing import parse_pointing
from .prompts import COMPANION_SYSTEM_PROMPT
from .state import Conversation, VoiceState
from .stt.base import STTEngine, STTError
from .stt.factory import make_stt
from .tts.base import TTSEngine
from .tts.factory import make_tts
from .tts.streaming import SentenceSpeaker
from .ui.overlay import OverlayManager, PointTarget

log = logging.getLogger(__name__)

# Below this length we treat the press as an accidental tap and skip the model.
MIN_UTTERANCE_SECONDS = 0.35
# Let the compositor paint the "overlay hidden" frame before grabbing pixels.
CAPTURE_SETTLE_SECONDS = 0.12

ERROR_SPEECH = "i couldn't reach the model just now. check your provider settings in the panel."


class Pipeline(QObject):
    """Qt-facing controller for the whole companion workflow."""

    state_changed = pyqtSignal(object)  # VoiceState
    audio_level = pyqtSignal(float)
    transcript_ready = pyqtSignal(str)
    reply_ready = pyqtSignal(str)
    reply_delta = pyqtSignal(str)  # streamed chunks, for the chat view
    point_requested = pyqtSignal(object)  # PointTarget
    status_changed = pyqtSignal(str)
    error = pyqtSignal(str)
    listened = pyqtSignal(bool)

    # Internal signals used to hop from a worker thread onto the GUI thread.
    _hide_overlay_requested = pyqtSignal()
    _show_overlay_requested = pyqtSignal()
    _start_requested = pyqtSignal()
    _toggle_requested = pyqtSignal()
    _stop_requested = pyqtSignal()
    _state_requested = pyqtSignal(object)
    _max_reached = pyqtSignal()
    _hide_later_requested = pyqtSignal()

    def __init__(
        self,
        config: Config,
        overlay: OverlayManager,
        *,
        stt_factory: Callable[[Config], STTEngine] = make_stt,
        tts_factory: Callable[[Config], TTSEngine] = make_tts,
        provider_factory: Callable[[Config, str | None], LLMProvider] = make_provider,
        capture_service: ScreenCaptureService | None = None,
        hotkey: HotkeyBackend | None = None,
        welcome_on_start: bool = True,
    ) -> None:
        super().__init__()
        self.config = config
        self.overlay = overlay
        self._stt_factory = stt_factory
        self._tts_factory = tts_factory
        self._provider_factory = provider_factory
        self.capture = capture_service or ScreenCaptureService(config)
        self.hotkey = hotkey or resolve_hotkey_backend(config)

        self.history = Conversation(max_exchanges=10)
        self.state = VoiceState.IDLE
        self.last_transcript = ""
        self.last_reply = ""
        self.last_error = ""
        self._welcome_on_start = welcome_on_start
        self._started = False
        self._first_show = True
        self._point_active = False
        self._point_handler: Callable[[PointTarget], bool] | None = None

        self._stt: STTEngine | None = None
        self._tts: TTSEngine | None = None
        self._provider: LLMProvider | None = None
        self._provider_id = str(config.get("llm.provider", "groq"))

        self._recorder: MicrophoneRecorder | None = None
        self._cancel = threading.Event()
        self._generation = 0
        self._worker: threading.Thread | None = None
        self._hide_event = threading.Event()

        self._hide_overlay_requested.connect(self._hide_overlay_slot)
        self._show_overlay_requested.connect(self._show_overlay_slot)
        self._start_requested.connect(self.start_listening)
        self._toggle_requested.connect(self.toggle_listening)
        self._stop_requested.connect(self.stop_listening)
        self._state_requested.connect(self._apply_state)
        self._max_reached.connect(self._handle_max_recording)
        self._hide_later_requested.connect(self._schedule_transient_hide)
        self.point_requested.connect(self._fly_to_target)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._ensure_engines()
        self.overlay.rebuild()
        # Keep the transparent overlay mapped for the whole session. On Wayland
        # a window that is hidden and shown again can lose its stacking above
        # maximised windows, which would make the listening glow invisible.
        self.overlay.show()

        if self._welcome_on_start and self._first_show:
            self.overlay.welcome()
            self._first_show = False

        try:
            self.hotkey.start(self._on_hotkey_press, self._on_hotkey_release)
            if getattr(self.hotkey, "note", ""):
                log.warning("shortcut: %s", self.hotkey.note)
        except (HotkeyError, OSError) as exc:
            self._report_error(f"global shortcut unavailable: {exc}")
        self.status_changed.emit(self.shortcut_hint())

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.cancel()
        try:
            self.hotkey.stop()
        except Exception:  # pragma: no cover - backend specific cleanup
            log.debug("hotkey stop failed", exc_info=True)
        if self._recorder is not None:
            try:
                self._recorder.cancel()
            except Exception:
                pass
            self._recorder = None
        self.overlay.destroy()

    def shortcut_hint(self) -> str:
        if self.hotkey.id == "manual":
            return "use the panel button to start and stop listening"
        label = hotkey_label("+".join(self.hotkey.combo))
        if self.hotkey.id == "gnome":
            # The GNOME backend installs modifiers + Space as the accelerator.
            label = f"{label}+Space"
        if getattr(self.hotkey, "toggle_mode", False):
            return f"press {label} to start and stop talking"
        return f"hold {label} to talk"

    # ------------------------------------------------------------------
    # Engine management
    # ------------------------------------------------------------------
    def _ensure_engines(self) -> None:
        if self._stt is None:
            self._stt = self._stt_factory(self.config)
        if self._tts is None:
            self._tts = self._tts_factory(self.config)
        if self._provider is None:
            self._provider = self._provider_factory(self.config, self._provider_id)

    def reload_engines(self) -> None:
        """Re-read config after the settings dialog changed providers/keys."""
        self._stt = None
        self._tts = None
        self._provider = None
        self._provider_id = str(self.config.get("llm.provider", "groq"))
        self._ensure_engines()
        self.status_changed.emit(self.shortcut_hint())

    def set_provider(self, provider_id: str, model: str | None = None) -> None:
        provider_id = provider_id.lower()
        self._provider_id = provider_id
        self.config.set("llm.provider", provider_id)
        if model:
            self.config.set(f"llm.{provider_id}.model", model)
        self.config.save()
        self._provider = self._provider_factory(self.config, provider_id)
        label = LLM_DISPLAY_NAMES.get(provider_id, provider_id.title())
        self.status_changed.emit(f"{label} · {self._provider.model}")

    def apply_tts_volume(self, volume: float) -> None:
        self.config.set("tts.volume", volume)
        if self._tts is not None:
            self._tts.volume = max(0.0, min(1.0, volume))

    @property
    def provider(self) -> LLMProvider | None:
        self._ensure_engines()
        return self._provider

    @property
    def stt(self) -> STTEngine | None:
        self._ensure_engines()
        return self._stt

    @property
    def tts(self) -> TTSEngine | None:
        self._ensure_engines()
        return self._tts

    def restart_hotkey(self) -> None:
        if not self._started:
            return
        try:
            self.hotkey.stop()
        except Exception:
            pass
        self.hotkey = resolve_hotkey_backend(self.config)
        try:
            self.hotkey.start(self._on_hotkey_press, self._on_hotkey_release)
            self.status_changed.emit(self.shortcut_hint())
        except (HotkeyError, OSError) as exc:
            self._report_error(f"global shortcut unavailable: {exc}")

    # ------------------------------------------------------------------
    # Push-to-talk
    # ------------------------------------------------------------------
    def toggle_listening(self) -> None:
        if self.state is VoiceState.LISTENING:
            self.stop_listening()
        else:
            self.start_listening()

    def _on_hotkey_press(self) -> None:
        if getattr(self.hotkey, "toggle_mode", False):
            self._toggle_requested.emit()
        else:
            self._start_requested.emit()

    def _on_hotkey_release(self) -> None:
        if getattr(self.hotkey, "toggle_mode", False):
            return
        self._stop_requested.emit()

    def start_listening(self) -> None:
        if self.state is VoiceState.LISTENING:
            return
        # A new press interrupts any response that is still in flight, exactly
        # like the macOS app cancelling currentResponseTask and TTS playback.
        self.cancel()
        self._point_active = False
        self.overlay.clear_point()
        self.overlay.hide_prompt()

        if not self.overlay.visible:
            self.overlay.show()
        if self._first_show:
            self._first_show = False

        recorder = MicrophoneRecorder(
            sample_rate=int(self.config.get("audio.sample_rate", 16000)),
            device=str(self.config.get("audio.input_device", "default") or "default"),
            max_seconds=float(self.config.get("audio.max_seconds", 120)),
            on_level=self.audio_level.emit,
            on_max_reached=self._on_max_recording,
        )
        try:
            recorder.start()
        except Exception as exc:
            self._report_error(str(exc))
            self._set_state(VoiceState.IDLE)
            self._schedule_transient_hide()
            return

        self._recorder = recorder
        self.last_error = ""
        self._set_state(VoiceState.LISTENING)
        self._set_status("listening…")
        self.listened.emit(True)

    def stop_listening(self) -> None:
        recorder = self._recorder
        if recorder is None or self.state is not VoiceState.LISTENING:
            return
        self._recorder = None
        try:
            wav = recorder.stop()
        except Exception as exc:
            self._report_error(f"microphone capture failed: {exc}")
            self._set_state(VoiceState.IDLE)
            self._schedule_transient_hide()
            return

        self.listened.emit(False)
        duration = wav_duration_seconds(wav)
        if duration < MIN_UTTERANCE_SECONDS:
            self._set_state(VoiceState.IDLE)
            self._set_status("ready")
            self._schedule_transient_hide()
            return

        # Snapshot Qt screen geometry on the GUI thread; the worker will only
        # read these plain dataclasses.
        try:
            screens = self.capture.screens_provider()
            cursor = self.capture.cursor_provider()
        except Exception:
            screens = []
            cursor = (0.0, 0.0)
        self._begin_response(wav, screens, cursor)

    def _on_max_recording(self) -> None:
        self._max_reached.emit()

    def _handle_max_recording(self) -> None:
        self.audio_level.emit(0.0)
        self.stop_listening()

    def cancel(self) -> None:
        """Abort the current recording, model stream or playback."""
        self._generation += 1
        self._cancel.set()
        tts = self._tts
        if tts is not None:
            try:
                tts.stop()
            except Exception:
                log.debug("tts stop failed", exc_info=True)
        recorder = self._recorder
        if recorder is not None:
            self._recorder = None
            try:
                recorder.cancel()
            except Exception:
                pass
        self.overlay.cancel_navigation()
        self._point_active = False

    # ------------------------------------------------------------------
    # Response worker
    # ------------------------------------------------------------------
    def _begin_response(self, wav: bytes, screens: list[ScreenInfo], cursor: tuple[float, float]) -> None:
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        self._set_state(VoiceState.PROCESSING)
        self._set_status("thinking…")

        worker = threading.Thread(
            target=self._run_response,
            args=(generation, wav, screens, cursor, self._cancel),
            name="heyclicky-response",
            daemon=True,
        )
        self._worker = worker
        worker.start()

    def _run_response(
        self,
        generation: int,
        wav: bytes,
        screens: list[ScreenInfo],
        cursor: tuple[float, float],
        cancel: threading.Event,
    ) -> None:
        try:
            self._respond(generation, wav, screens, cursor, cancel)
        except StreamCancelled:
            pass
        except Exception as exc:  # never let a worker die silently
            log.exception("response pipeline failed")
            if self._is_current(generation):
                self._report_error(_friendly_error(exc))
        finally:
            if self._is_current(generation):
                self._set_state(VoiceState.IDLE)
                self._set_status("ready")
                self._schedule_transient_hide()

    def _respond(
        self,
        generation: int,
        wav: bytes,
        screens: list[ScreenInfo],
        cursor: tuple[float, float],
        cancel: threading.Event,
    ) -> None:
        stt = self.stt
        assert stt is not None
        try:
            transcript = stt.transcribe(
                wav,
                keyterms=list(self.config.get("stt.keyterms", []) or []),
                language=str(self.config.get("stt.language", "en") or "en"),
            )
        except STTError as exc:
            if self._is_current(generation):
                self._report_error(f"transcription failed: {exc}")
            return
        transcript = STTEngine.clean(transcript)
        if cancel.is_set() or not self._is_current(generation):
            return

        self.last_transcript = transcript
        self.transcript_ready.emit(transcript)
        if not transcript:
            if self._is_current(generation):
                self._report_error("i didn't catch that. try holding the shortcut and speaking again.")
            return

        # Hide our own overlay before grabbing pixels; wait until the GUI
        # thread confirms the windows are hidden.
        self._hide_event.clear()
        self._hide_overlay_requested.emit()
        self._hide_event.wait(timeout=1.5)
        time.sleep(CAPTURE_SETTLE_SECONDS)

        bundle: CaptureBundle
        try:
            bundle = self.capture.capture_all(screens=screens, cursor=cursor)
        except CaptureError as exc:
            if self._is_current(generation):
                self._report_error(f"screen capture failed: {exc}")
            return
        finally:
            self._show_overlay_requested.emit()

        if cancel.is_set() or not self._is_current(generation):
            return

        provider = self.provider
        assert provider is not None
        images = [
            ImageAttachment(data=image.data, mime_type=image.mime_type, label=image.label)
            for image in bundle.images
        ]
        request = ChatRequest(
            system_prompt=COMPANION_SYSTEM_PROMPT,
            user_text=transcript,
            images=images,
            history=list(self.history.exchanges),
            max_tokens=int(self.config.get("llm.max_tokens", 1024)),
            temperature=float(self.config.get("llm.temperature", 0.4)),
        )

        def _speaking_started() -> None:
            self._set_state(VoiceState.RESPONDING)
            self._set_status("speaking…")

        tts = self.tts
        speaker: SentenceSpeaker | None = None
        if tts is not None and bool(self.config.get("tts.enabled", True)):
            speaker = SentenceSpeaker(tts, cancel=cancel, on_start=_speaking_started)
            speaker.start()

        saw_delta = False

        def on_delta(text: str) -> None:
            nonlocal saw_delta
            if cancel.is_set():
                raise StreamCancelled()
            saw_delta = True
            # Paint the answer while the model is still writing it, and start
            # speaking after the first complete sentence.
            self.reply_delta.emit(text)
            if speaker is not None:
                speaker.feed(text)

        try:
            full_response = ""
            for _attempt in range(2):
                try:
                    full_response = provider.stream_chat(request, on_delta=on_delta, cancel=cancel)
                except StreamCancelled:
                    if speaker is not None:
                        speaker.stop()
                    return
                except ProviderError as exc:
                    if speaker is not None:
                        speaker.stop()
                    if self._is_current(generation):
                        self._report_error(str(exc))
                        self._speak_error(generation, cancel)
                    return
                if full_response.strip() or saw_delta or cancel.is_set():
                    break
                # Some free endpoints occasionally return an empty stream; one
                # quiet retry is much better than an empty chat bubble.
                log.warning("empty reply from %s, retrying once", provider.id)
            if speaker is not None and not cancel.is_set():
                speaker.finish()
        finally:
            if speaker is not None and cancel.is_set():
                speaker.stop()

        if cancel.is_set() or not self._is_current(generation):
            return

        parsed = parse_pointing(full_response)
        spoken = parsed.spoken_text.strip()
        self.last_reply = spoken
        self.reply_ready.emit(spoken)

        if spoken:
            self.history.add(transcript, spoken)

        target: PointTarget | None = None
        if parsed.has_coordinate and parsed.coordinate is not None:
            target = self._point_target(parsed, bundle)
        if target is not None and self._is_current(generation):
            self._point_active = True
            self.point_requested.emit(target)

        if speaker is not None:
            if spoken:
                self._set_state(VoiceState.RESPONDING)
                self._set_status("speaking…")
                speaker.join()
            else:
                speaker.stop()
            if self._is_current(generation):
                self._set_state(VoiceState.IDLE)
                self._set_status("ready")

    def _point_target(self, parsed: Any, bundle: CaptureBundle) -> PointTarget | None:
        if not bundle.images:
            return None
        cursor_image = bundle.cursor_image or bundle.images[0]
        screen_number = parsed.screen_number
        if screen_number is not None and 1 <= screen_number <= len(bundle.images):
            image = bundle.images[screen_number - 1]
        else:
            image = cursor_image
            screen_number = bundle.images.index(image) + 1

        # Guard against a race where the model names a screen that no longer
        # matches the geometry snapshot.
        geometries = [entry.screen.geometry for entry in bundle.images]
        screen = choose_target_screen(geometries, screen_number, cursor_image.screen.geometry)
        if screen is None:
            return None

        x, y = parsed.coordinate
        global_x, global_y = map_screenshot_point(
            screenshot_x=x,
            screenshot_y=y,
            screenshot_width=image.pixel_width,
            screenshot_height=image.pixel_height,
            screen=screen,
        )
        label = parsed.element_label
        return PointTarget(
            global_x=global_x,
            global_y=global_y,
            screen_number=screen_number,
            bubble_text="",
            label=label,
        )

    def _speak_error(self, generation: int, cancel: threading.Event) -> None:
        tts = self._tts
        if tts is None or not bool(self.config.get("tts.enabled", True)):
            return
        if not self._is_current(generation):
            return
        try:
            tts.speak(ERROR_SPEECH, cancel=cancel)
        except Exception:
            log.debug("error speech failed", exc_info=True)

    # ------------------------------------------------------------------
    # Overlay bridge
    # ------------------------------------------------------------------
    def _hide_overlay_slot(self) -> None:
        try:
            self.overlay.hide_for_capture()
        finally:
            self._hide_event.set()

    def _show_overlay_slot(self) -> None:
        self.overlay.show_after_capture()

    def set_point_handler(self, handler: Callable[[PointTarget], bool] | None) -> None:
        """Install a handler that may render a point itself (return ``True``).

        The panel uses this to draw pointing rings inside its own window, which
        a screen-wide overlay cannot cover on Wayland.
        """
        self._point_handler = handler

    def _fly_to_target(self, target: PointTarget) -> None:
        if self._point_handler is not None and self._point_handler(target):
            # Something else (for example Clicky's own panel) rendered the
            # marker, so skip the screen overlay and close the lifecycle.
            self.on_point_finished()
            return
        self.overlay.point_at(target, [])

    def on_point_finished(self) -> None:
        self._point_active = False
        self._schedule_transient_hide()

    def _schedule_transient_hide(self) -> None:
        """No-op: the overlay stays mapped and idle simply paints nothing.

        Keeping it mapped matters on Wayland, where a full-screen overlay that
        is unmapped and mapped again can fall behind maximised windows.
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _is_current(self, generation: int) -> bool:
        return generation == self._generation and self._started

    def _set_state(self, state: VoiceState) -> None:
        if QThread.currentThread() is not self.thread():
            self._state_requested.emit(state)
            return
        self._apply_state(state)

    def _apply_state(self, state: VoiceState) -> None:
        if self.state is state:
            return
        self.state = state
        self.overlay.set_state(state)
        self.state_changed.emit(state)
        if state is VoiceState.IDLE:
            self._schedule_transient_hide()

    def _set_status(self, text: str) -> None:
        self.status_changed.emit(text)

    def _report_error(self, message: str) -> None:
        self.last_error = message
        log.warning("%s", message)
        self.error.emit(message)

    # ------------------------------------------------------------------
    # Welcome / diagnostics
    # ------------------------------------------------------------------
    def show_welcome(self) -> None:
        if not self.overlay.visible:
            self.overlay.show()
        self.overlay.welcome()

    def speak_welcome(self) -> None:
        """Optional spoken greeting used from the tray menu."""
        tts = self.tts
        if tts is None:
            return
        cancel = threading.Event()
        thread = threading.Thread(
            target=lambda: _safe_speak(tts, "hey, i'm clicky. hold the shortcut and ask me anything.", cancel),
            name="heyclicky-welcome-tts",
            daemon=True,
        )
        thread.start()

    def capture_once(self):
        """Capture without an utterance; used by ``--capture-test``."""
        return self.capture.capture_all()


def _safe_speak(tts: TTSEngine, text: str, cancel: threading.Event) -> None:
    try:
        tts.speak(text, cancel=cancel)
    except Exception:
        log.debug("welcome speech failed", exc_info=True)


def _friendly_error(exc: Exception) -> str:
    text = str(exc).strip()
    if isinstance(exc, ProviderError):
        return text
    if isinstance(exc, CaptureError):
        return f"screen capture failed: {text}"
    if isinstance(exc, STTError):
        return f"transcription failed: {text}"
    return f"something went wrong: {text or exc.__class__.__name__}"
