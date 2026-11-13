"""Transparent, click-through overlay for screen state and pointing.

One window is created per display, mirroring the macOS OverlayWindowManager.
There is no cursor-following buddy: while Clicky is active the whole screen gets
a soft blue edge glow (audio-reactive while listening), and when the assistant
points at something a pulsing ring marks the element for a few seconds.

The flight math is kept from OverlayWindow.swift: 0.6-1.4s quadratic Bezier
arcs with an 80px cap, and a three-second hold on the pointing bubble.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import QWidget

from ..geometry import bezier_point, flight_plan, smoothstep
from ..state import VoiceState
from . import theme

BUBBLE_OFFSET = (10.0, 18.0)
POINTER_PHRASES = ("right here!", "this one!", "over here!", "click this!", "here it is!", "found it!")


def glow_intensity(state: VoiceState, audio_level: float, phase: float) -> float:
    """Brightness of the screen-edge glow, from 0 (off) to 1 (bright).

    ``phase`` advances with time so the border breathes while active; the
    microphone level makes the glow react while listening.
    """
    if state is VoiceState.IDLE:
        return 0.0
    pulse = (math.sin(phase) + 1.0) / 2.0
    if state is VoiceState.LISTENING:
        level = max(0.0, min(1.0, audio_level))
        return min(1.0, 0.45 + 0.25 * pulse + 0.45 * level)
    if state is VoiceState.PROCESSING:
        return min(1.0, 0.30 + 0.18 * pulse)
    return min(1.0, 0.25 + 0.10 * pulse)


@dataclass
class PointTarget:
    global_x: float
    global_y: float
    screen_number: int | None
    bubble_text: str
    label: str | None = None


class OverlayWindow(QWidget):
    def __init__(self, screen, manager: OverlayManager) -> None:
        super().__init__(None)
        self.manager = manager
        self.qscreen = screen
        self._screen_geometry = screen.geometry()
        self._device_ratio = max(1.0, float(screen.devicePixelRatio()))

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(False)

        self._screen_origin = QPointF(self._screen_geometry.x(), self._screen_geometry.y())
        # Only used as the anchor for prompt/welcome bubbles now; there is no
        # cursor-following buddy any more.
        self._position = QPointF(
            self._screen_geometry.width() / 2.0,
            self._screen_geometry.height() / 2.0,
        )
        self._target = QPointF(self._position)
        self._master_opacity = 1.0

        # navigation state
        self._mode = "idle"
        self._flight_start = 0.0
        self._flight_duration = 0.0
        self._flight_from = QPointF()
        self._flight_to = QPointF()
        self._flight_control = QPointF()
        self._flight_timer: QTimer | None = None
        self._point_target: PointTarget | None = None
        self._target_ring_opacity = 0.0
        self._glow_started = time.monotonic()

        # bubbles
        self._welcome_text = ""
        self._welcome_opacity = 0.0
        self._show_welcome = False
        self._prompt_text = ""
        self._prompt_opacity = 0.0
        self._point_bubble_text = ""
        self._point_bubble_opacity = 0.0
        self._point_bubble_scale = 1.0

        self._voice_state = VoiceState.IDLE
        self._audio_level = 0.0
        self._suspended = False

        self._track_timer = QTimer(self)
        self._track_timer.setInterval(16)
        self._track_timer.timeout.connect(self._tick)
        self._track_timer.start()

        self._char_timer = QTimer(self)
        self._char_timer.setSingleShot(True)
        self._char_timer.timeout.connect(self._stream_next_character)
        self._char_index = 0
        self._char_phrase = ""
        self._char_done = None

        self._place_on_screen()

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------
    def _place_on_screen(self) -> None:
        geometry = self._screen_geometry
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            # Wayland compositors decide window placement. setScreen + full
            # screen is the closest a normal client can get without layer-shell.
            handle = self.windowHandle()
            if handle is not None:
                handle.setScreen(self.qscreen)
            self.showFullScreen()
        else:
            self.setGeometry(geometry)
            self.show()

    def update_screen_geometry(self) -> None:
        geometry = self.qscreen.geometry()
        if geometry == self._screen_geometry:
            return
        self._screen_geometry = geometry
        self._screen_origin = QPointF(geometry.x(), geometry.y())
        if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
            self.setGeometry(geometry)
        self.update()

    # ------------------------------------------------------------------
    # Public controls used by OverlayManager
    # ------------------------------------------------------------------
    def set_voice_state(self, state: VoiceState) -> None:
        if state is not self._voice_state and state is not VoiceState.IDLE:
            self._glow_started = time.monotonic()
        self._voice_state = state
        if state is not VoiceState.IDLE:
            self._start_tracking()
        self.update()

    def set_audio_level(self, level: float) -> None:
        self._audio_level = max(0.0, min(1.0, level))

    def set_master_opacity(self, opacity: float) -> None:
        self._master_opacity = max(0.0, min(1.0, opacity))
        self.update()

    def show_welcome(self) -> None:
        self._show_welcome = True
        self._welcome_text = ""
        self._welcome_opacity = 0.0
        QTimer.singleShot(600, self._begin_welcome_stream)

    def _begin_welcome_stream(self) -> None:
        if not self._show_welcome:
            return
        self._welcome_opacity = 1.0
        self._stream_text("hey! i'm clicky", self._append_welcome_character, self._finish_welcome)

    def _append_welcome_character(self, character: str) -> None:
        self._welcome_text += character
        self.update()

    def _finish_welcome(self) -> None:
        QTimer.singleShot(2000, self._fade_welcome)

    def _fade_welcome(self) -> None:
        for step in range(10, -1, -1):
            QTimer.singleShot((10 - step) * 50, lambda value=step: self._set_welcome_opacity(value / 10))
        QTimer.singleShot(600, self._hide_welcome)

    def _set_welcome_opacity(self, value: float) -> None:
        self._welcome_opacity = value
        self.update()

    def _hide_welcome(self) -> None:
        self._show_welcome = False
        self._welcome_text = ""
        self.update()
        self.manager.maybe_hide()

    def show_prompt(self, text: str) -> None:
        self._prompt_text = ""
        self._prompt_opacity = 1.0
        self._stream_text(text, self._append_prompt_character, lambda: QTimer.singleShot(10000, self.hide_prompt))

    def _append_prompt_character(self, character: str) -> None:
        self._prompt_text += character
        self.update()

    def hide_prompt(self) -> None:
        self._prompt_opacity = 0.0
        self._prompt_text = ""
        self.update()
        self.manager.maybe_hide()

    def navigate_to(self, target: PointTarget) -> None:
        self._point_target = target
        global_point = (target.global_x, target.global_y)
        screen = self._screen_geometry
        local = (global_point[0] - screen.x(), global_point[1] - screen.y())
        destination = (local[0] + 8.0, local[1] + 12.0)
        destination = (
            max(20.0, min(destination[0], screen.width() - 20.0)),
            max(20.0, min(destination[1], screen.height() - 20.0)),
        )
        self._mode = "navigating"
        self._animate_flight(destination, self._start_pointing)
        self._start_tracking()

    def cancel_navigation(self) -> None:
        self._stop_flight()
        self._point_animation_token += 1
        self.reset_navigation()

    def reset_navigation(self) -> None:
        self._stop_flight()
        self._mode = "idle"
        self._point_bubble_text = ""
        self._point_bubble_opacity = 0.0
        self._point_bubble_scale = 1.0
        self._target_ring_opacity = 0.0
        self.update()

    _point_animation_token = 0

    # ------------------------------------------------------------------
    # Tracking and animation
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        # Only the glow and the pointing ring animate continuously; bubbles
        # repaint themselves while streaming. Stop the 60Hz timer when idle so
        # a permanently mapped full-screen overlay costs nothing.
        if not self._master_opacity or not self._animating():
            self._track_timer.stop()
            return
        self.update()

    def _animating(self) -> bool:
        return self._mode in ("navigating", "pointing") or (
            self._voice_state is not VoiceState.IDLE and self.manager.edge_glow
        )

    def _has_content(self) -> bool:
        """True when there is anything at all to draw on this screen."""
        if self._mode in ("navigating", "pointing"):
            return True
        if self._show_welcome or self._prompt_text:
            return True
        return self._voice_state is not VoiceState.IDLE and self.manager.edge_glow

    def _start_tracking(self) -> None:
        if not self._track_timer.isActive():
            self._track_timer.start()

    def _animate_flight(self, destination: tuple[float, float], on_complete) -> None:
        self._stop_flight()
        start = (self._position.x(), self._position.y())
        control, duration, _arc = flight_plan(start, destination)
        self._flight_from = QPointF(*start)
        self._flight_to = QPointF(*destination)
        self._flight_control = QPointF(*control)
        self._flight_duration = duration
        self._flight_start = time.monotonic()
        self._flight_on_complete = on_complete
        self._flight_timer = QTimer(self)
        self._flight_timer.setInterval(16)
        self._flight_timer.timeout.connect(self._flight_step)
        self._flight_timer.start()

    def _stop_flight(self) -> None:
        if self._flight_timer is not None:
            self._flight_timer.stop()
            self._flight_timer.deleteLater()
            self._flight_timer = None

    def _flight_step(self) -> None:
        elapsed = time.monotonic() - self._flight_start
        linear = min(1.0, elapsed / max(0.001, self._flight_duration))
        t = smoothstep(linear)
        x, y = bezier_point(
            (self._flight_from.x(), self._flight_from.y()),
            (self._flight_control.x(), self._flight_control.y()),
            (self._flight_to.x(), self._flight_to.y()),
            t,
        )
        self._position = QPointF(x, y)
        if linear >= 1.0:
            self._position = self._flight_to
            self._stop_flight()
            callback = getattr(self, "_flight_on_complete", None)
            if callback is not None:
                callback()

    def _start_pointing(self) -> None:
        self._mode = "pointing"
        self._target_ring_opacity = 1.0
        self._point_bubble_text = ""
        self._point_bubble_opacity = 1.0
        self._point_bubble_scale = 0.5
        self._point_animation_token += 1
        phrase = self._point_target.bubble_text if self._point_target else ""
        if not phrase:
            phrase = random.choice(POINTER_PHRASES)
        self._stream_text(phrase, self._append_point_character, self._hold_pointing)

    def _append_point_character(self, character: str) -> None:
        if not self._point_bubble_text:
            self._point_bubble_scale = 1.0
        self._point_bubble_text += character
        self.update()

    def _hold_pointing(self) -> None:
        token = self._point_animation_token
        QTimer.singleShot(3000, lambda: self._fade_pointing(token))

    def _fade_pointing(self, token: int) -> None:
        if token != self._point_animation_token or self._mode != "pointing":
            return
        self._target_ring_opacity = 0.0
        self._point_bubble_opacity = 0.0
        QTimer.singleShot(400, lambda: self._finish_navigation(token))

    def _finish_navigation(self, token: int | None = None) -> None:
        if token is not None and token != self._point_animation_token:
            return
        self.reset_navigation()
        self.manager.point_finished()

    # ------------------------------------------------------------------
    # Text streaming
    # ------------------------------------------------------------------
    def _stream_text(self, phrase: str, on_character, on_complete) -> None:
        self._char_phrase = phrase
        self._char_index = 0
        self._char_on_character = on_character
        self._char_done = on_complete
        self._char_timer.start(30)

    def _stream_next_character(self) -> None:
        if self._char_index >= len(self._char_phrase):
            callback = self._char_done
            self._char_done = None
            if callback is not None:
                callback()
            return
        character = self._char_phrase[self._char_index]
        self._char_index += 1
        self._char_on_character(character)
        # Variable 30-60ms cadence, like the Swift character streamer.
        self._char_timer.start(random.randint(30, 60))

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._master_opacity <= 0:
            return
        if self._suspended or not self._has_content():
            # Explicitly clear the translucent backing store. Simply skipping
            # the paint leaves the last glow frame on screen, which would end
            # up in screenshots even though the overlay is "hidden".
            painter = QPainter(self)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear
            )
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            painter.end()
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setOpacity(self._master_opacity)

        self._draw_edge_glow(painter)

        anchor = QPointF(self._position)
        if self._show_welcome and self._welcome_text:
            self._draw_bubble(painter, self._welcome_text, anchor, self._welcome_opacity, 1.0)
        if self._prompt_text:
            self._draw_bubble(painter, self._prompt_text, anchor, self._prompt_opacity, 1.0)
        if self._mode in ("navigating", "pointing"):
            self._draw_target_ring(painter, anchor)
            if self._point_bubble_text:
                self._draw_bubble(
                    painter,
                    self._point_bubble_text,
                    anchor,
                    self._point_bubble_opacity,
                    self._point_bubble_scale,
                )
        painter.end()

    def _draw_edge_glow(self, painter: QPainter) -> None:
        """Soft blue border around the whole screen while Clicky is active."""
        if not self.manager.edge_glow:
            return
        phase = (time.monotonic() - self._glow_started) * 6.0
        intensity = glow_intensity(self._voice_state, self._audio_level, phase)
        if intensity <= 0.0:
            return
        base = theme.color("overlay_cursor_blue")
        rect = QRectF(self.rect())
        # Layered outlines approximate a blurred neon edge without shaders.
        for width, alpha in ((28.0, 0.10), (16.0, 0.18), (7.0, 0.38), (2.0, 0.75)):
            stroke = QColor(base)
            stroke.setAlphaF(min(1.0, alpha * intensity))
            pen = QPen(stroke)
            pen.setWidthF(width)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            inset = width / 2.0
            painter.drawRoundedRect(
                rect.adjusted(inset, inset, -inset, -inset), 18.0, 18.0
            )

    def _draw_target_ring(self, painter: QPainter, position: QPointF) -> None:
        """A pulsing ring marks the pointed-at element (no cursor buddy)."""
        if self._target_ring_opacity <= 0.0:
            return
        pulse = (math.sin((time.monotonic() - self._glow_started) * 5.0) + 1.0) / 2.0
        base = theme.color("overlay_cursor_blue")
        painter.save()
        painter.translate(position)
        for radius, alpha in ((20.0 + pulse * 6.0, 0.12), (12.0, 0.32), (5.0, 0.9)):
            stroke = QColor(base)
            stroke.setAlphaF(min(1.0, alpha * self._target_ring_opacity))
            pen = QPen(stroke)
            pen.setWidthF(2.0 if radius > 8 else 2.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), radius, radius)
        painter.restore()

    def _draw_bubble(
        self,
        painter: QPainter,
        text: str,
        anchor: QPointF,
        opacity: float,
        scale: float,
    ) -> None:
        if not text or opacity <= 0:
            return
        font = theme.font(9, QFont.Weight.Medium)
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        text_height = metrics.height()
        width = text_width + 16
        height = text_height + 8
        left = anchor.x() + BUBBLE_OFFSET[0]
        top = anchor.y() + BUBBLE_OFFSET[1] - height / 2
        # Keep the bubble on screen.
        left = max(4.0, min(left, self.width() - width - 4))
        top = max(4.0, min(top, self.height() - height - 4))
        rect = QRectF(left, top, width, height)

        painter.save()
        painter.setOpacity(opacity * self._master_opacity)
        painter.translate(rect.center())
        painter.scale(scale, scale)
        painter.translate(-rect.center())

        base = theme.color("overlay_cursor_blue")
        # Layered translucent fills approximate the SwiftUI shadow glow.
        for spread, alpha in ((8.0, 0.10), (4.0, 0.18), (0.0, 1.0)):
            fill = QColor(base)
            fill.setAlphaF(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(
                QRectF(rect.x() - spread / 2, rect.y() - spread / 2, rect.width() + spread, rect.height() + spread),
                6.0 + spread / 2,
                6.0 + spread / 2,
            )

        painter.setPen(theme.color("text_primary"))
        painter.setFont(font)
        text_rect = QRectF(rect.x() + 8, rect.y() + 4, rect.width() - 16, rect.height() - 8)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        painter.restore()


class OverlayManager:
    """Creates one OverlayWindow per QScreen and coordinates point animation."""

    def __init__(self, on_point_finished=None, config=None) -> None:
        self.windows: list[OverlayWindow] = []
        self.visible = False
        self._on_point_finished = on_point_finished
        self._target_screen: int | None = None
        self._state = VoiceState.IDLE
        self._audio_level = 0.0
        self.config = config
        self.edge_glow = True if config is None else bool(
            config.get("ui.edge_glow", True)
        )
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(lambda _screen: self.rebuild())
            app.screenRemoved.connect(lambda _screen: self.rebuild())

    def set_point_finished_callback(self, callback) -> None:
        self._on_point_finished = callback

    def set_edge_glow(self, enabled: bool) -> None:
        self.edge_glow = bool(enabled)
        for window in self.windows:
            window.update()

    # -- window lifecycle ----------------------------------------------
    def rebuild(self) -> None:
        was_visible = self.visible
        self.destroy()
        for screen in QGuiApplication.screens():
            window = OverlayWindow(screen, self)
            window.set_voice_state(self._state)
            self.windows.append(window)
            if was_visible:
                window.show()
        self.visible = was_visible

    def show(self) -> None:
        if not self.windows:
            self.rebuild()
        for window in self.windows:
            # Cancel a fade-out still running from the previous idle period,
            # otherwise it hides the window again mid-listening.
            animation = getattr(window, "_fade_animation", None)
            if animation is not None:
                try:
                    animation.stop()
                except RuntimeError:
                    # Qt already deleted it (DeleteWhenStopped).
                    pass
                window._fade_animation = None
            window.setWindowOpacity(1.0)
            window.set_master_opacity(1.0)
            window.show()
        self.visible = True

    def hide(self) -> None:
        for window in self.windows:
            window.hide()
        self.visible = False

    def destroy(self) -> None:
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.windows = []
        self.visible = False

    def hide_for_capture(self) -> None:
        # Do not unmap the window: on Wayland a re-shown full-screen overlay can
        # end up behind maximised windows. Clearing the paint is enough, but it
        # has to happen synchronously so the compositor never grabs the glow.
        for window in self.windows:
            window._suspended = True
            window.update()
            window.repaint()

    def show_after_capture(self) -> None:
        for window in self.windows:
            window._suspended = False
            window.update()

    def should_stay_visible(self) -> bool:
        """True while the glow, a bubble or a pointing ring is on screen."""
        if self._state is not VoiceState.IDLE:
            return True
        return any(
            window._mode in ("navigating", "pointing")
            or window._show_welcome
            or bool(window._prompt_text)
            for window in self.windows
        )

    def maybe_hide(self) -> None:
        """Kept for callers: the overlay stays mapped, idle simply draws nothing."""
        for window in self.windows:
            window.update()

    def fade_out_and_hide(self) -> None:
        if not self.visible:
            return
        for window in self.windows:
            animation = QPropertyAnimation(window, b"windowOpacity", window)
            animation.setDuration(400)
            animation.setStartValue(1.0)
            animation.setEndValue(0.0)
            animation.finished.connect(lambda w=window: w.hide())
            animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            window._fade_animation = animation  # keep a reference alive
        self.visible = False

    # -- state ----------------------------------------------------------
    def set_state(self, state: VoiceState) -> None:
        self._state = state
        for window in self.windows:
            window.set_voice_state(state)

    def set_audio_level(self, level: float) -> None:
        self._audio_level = level
        for window in self.windows:
            window.set_audio_level(level)

    def welcome(self) -> None:
        window = self._primary_window()
        if window is not None:
            window.show_welcome()

    def prompt(self, text: str) -> None:
        window = self._primary_window()
        if window is not None:
            window.show_prompt(text)

    def _primary_window(self) -> OverlayWindow | None:
        if not self.windows:
            return None
        return self.windows[0]

    def point_at(self, target: PointTarget, screens_ordered) -> None:
        """Animate the target ring on the screen that owns the target."""
        for index, window in enumerate(self.windows):
            geometry = window._screen_geometry
            if geometry.contains(QPoint(int(target.global_x), int(target.global_y))):
                window.navigate_to(target)
                self._target_screen = index
                return
        # Target not found (coordinate mapping edge case) - use primary.
        if self.windows:
            self.windows[0].navigate_to(target)

    def clear_point(self) -> None:
        for window in self.windows:
            window.reset_navigation()
        self._target_screen = None

    def cancel_navigation(self) -> None:
        """Stop an in-flight navigation and drop the pointing ring."""
        for window in self.windows:
            window.cancel_navigation()
        self._target_screen = None

    def point_finished(self) -> None:
        self._target_screen = None
        if self._on_point_finished is not None:
            self._on_point_finished()

    def active_navigation(self) -> bool:
        return any(
            window._mode in ("navigating", "pointing") for window in self.windows
        )

    def hide_prompt(self) -> None:
        for window in self.windows:
            window.hide_prompt()
