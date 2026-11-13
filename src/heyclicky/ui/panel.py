"""The main panel and settings dialog.

The panel is a frameless top-level window rather than a real tray popover
because most Linux desktops do not expose the tray icon geometry.

It is split into two columns: every control (shortcut hint, model picker,
health, talk button, settings/quit) lives on the left, and a scrollable,
chat-style conversation lives on the right. The whole window can be dragged
from its title bar; on Wayland that goes through ``startSystemMove()`` because
compositors ignore programmatic ``move()`` calls for toplevel windows.
"""

from __future__ import annotations

import copy
import logging
import math
import shutil
import threading
import time
from collections.abc import Callable

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..audio import list_input_devices
from ..capture import CaptureError
from ..config import Config
from ..hotkey import fn_key_supported, hotkey_label
from ..llm.factory import DEFAULT_MODELS, DISPLAY_NAMES, PROVIDERS, make_provider
from ..pipeline import Pipeline
from ..state import VoiceState
from ..stt.factory import make_stt
from ..tts.factory import make_tts, tts_available
from . import theme

log = logging.getLogger(__name__)

PANEL_WIDTH = 900
PANEL_HEIGHT = 640
PANEL_MIN_WIDTH = 720
PANEL_MIN_HEIGHT = 500
CONTROLS_WIDTH = 340
PROVIDER_LABELS = {"openrouter": "OpenRouter", "groq": "Groq", "gemini": "Gemini", "ollama": "Local"}


class StatusDot(QWidget):
    """Small glowing dot; pulses while the companion is working."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = QColor(theme.COLORS["text_tertiary"])
        self._glow = 0.35
        self._timer = QTimer(self)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._pulse)
        self._phase = 0.0

    def set_status(self, state: VoiceState, visible: bool) -> None:
        if not visible:
            self._color = QColor(theme.COLORS["text_tertiary"])
            self._timer.stop()
        elif state is VoiceState.IDLE:
            self._color = QColor(theme.COLORS["success"])
            self._timer.stop()
        else:
            self._color = QColor(theme.COLORS["accent_text"])
            self._timer.start()
        self._glow = 0.55 if visible else 0.3
        self.update()

    def _pulse(self) -> None:
        import math

        self._phase += 0.22
        self._glow = 0.45 + (math.sin(self._phase) + 1.0) * 0.22
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QRectF(0, 0, self.width(), self.height()).center()
        gradient = QRadialGradient(center, 7.0)
        glow = QColor(self._color)
        glow.setAlphaF(min(0.65, self._glow))
        gradient.setColorAt(0.0, glow)
        gradient.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(4, 4, 6, 6))


class HealthRow(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(8)
        self._dot = QLabel("●")
        self._dot.setFixedWidth(10)
        self._label = QLabel(label)
        self._label.setObjectName("Secondary")
        self._detail = QLabel("checking…")
        self._detail.setObjectName("Tertiary")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._detail.setWordWrap(False)
        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addStretch(1)
        layout.addWidget(self._detail, 1)
        self.set_health(None, "checking…")

    def set_health(self, ok: bool | None, detail: str) -> None:
        if ok is True:
            colour = theme.COLORS["success"]
        elif ok is False:
            colour = theme.COLORS["warning"]
        else:
            colour = theme.COLORS["text_tertiary"]
        self._dot.setStyleSheet(f"color: {colour}; font-size: 10px;")
        short = detail if len(detail) <= 36 else detail[:33].rstrip() + "…"
        self._detail.setText(short)
        self._detail.setToolTip(detail)
        self._detail.setStyleSheet(
            f"color: {colour if ok is False else theme.COLORS['text_tertiary']}; font-size: 10px;"
        )


class _PointMarker(QWidget):
    """Pulsing ring drawn on top of the panel's own widgets.

    The panel is a separate always-on-top window. On Wayland a screen-wide
    overlay cannot be raised above another always-on-top client, so a pointing
    ring aimed at the Clicky panel would end up hidden behind it. In that case
    the panel draws the marker itself, using the same ring language.
    """

    HOLD_MS = 3200
    FADE_STEP_MS = 40

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._center = QPointF()
        self._label = ""
        self._started = 0.0
        self._opacity = 0.0
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._begin_fade)
        self._fade = QTimer(self)
        self._fade.setInterval(self.FADE_STEP_MS)
        self._fade.timeout.connect(self._fade_step)
        self._pulse = QTimer(self)
        self._pulse.setInterval(16)
        self._pulse.timeout.connect(self.update)
        self.hide()

    def show_at(self, x: float, y: float, label: str = "") -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self._center = QPointF(x, y)
        self._label = label
        self._opacity = 1.0
        self._started = time.monotonic()
        self._fade.stop()
        self.raise_()
        self.show()
        self._pulse.start()
        self._hold.start(self.HOLD_MS)

    def _begin_fade(self) -> None:
        self._fade.start()

    def _fade_step(self) -> None:
        self._opacity = max(0.0, self._opacity - 0.12)
        self.update()
        if self._opacity <= 0.0:
            self._fade.stop()
            self._pulse.stop()
            self._label = ""
            self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._opacity <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        base = theme.color("overlay_cursor_blue")
        pulse = (math.sin((time.monotonic() - self._started) * 5.0) + 1.0) / 2.0
        painter.save()
        painter.translate(self._center)
        for radius, alpha in ((20.0 + pulse * 6.0, 0.14), (12.0, 0.36), (5.0, 0.95)):
            stroke = QColor(base)
            stroke.setAlphaF(min(1.0, alpha * self._opacity))
            pen = QPen(stroke)
            pen.setWidthF(2.0 if radius > 8 else 2.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), radius, radius)
        painter.restore()

        if self._label:
            font = QFont(self.font())
            font.setPointSizeF(9.5)
            painter.setFont(font)
            metrics = QFontMetrics(font)
            text = metrics.elidedText(
                self._label, Qt.TextElideMode.ElideRight, self.width() - 40
            )
            width = metrics.horizontalAdvance(text) + 16
            height = metrics.height() + 8
            x = self._center.x() - width / 2
            y = self._center.y() + 30
            x = max(8.0, min(x, self.width() - width - 8.0))
            y = min(y, self.height() - height - 6.0)
            painter.setPen(Qt.PenStyle.NoPen)
            background = QColor(10, 12, 18)
            background.setAlphaF(min(1.0, 0.88 * self._opacity))
            painter.setBrush(background)
            painter.drawRoundedRect(QRectF(x, y, width, height), 7.0, 7.0)
            painter.setPen(QColor(235, 240, 255))
            painter.setOpacity(self._opacity)
            painter.drawText(
                QRectF(x, y, width, height),
                Qt.AlignmentFlag.AlignCenter,
                text,
            )


class _WindowButton(QPushButton):
    """Minimize/close glyphs painted by hand.

    A text glyph such as "×" gets squeezed by the global button padding and can
    end up rendering as a dot, so these controls draw their own lines.
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._hover = False
        self.setFixedSize(26, 26)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 26))
            painter.drawRoundedRect(area, 6, 6)

        colour = QColor(
            theme.COLORS["text_primary"] if self._hover else theme.COLORS["text_secondary"]
        )
        pen = QPen(colour)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        centre = area.center()
        if self._kind == "minimize":
            painter.drawLine(
                QPointF(centre.x() - 5.0, centre.y() + 1.0),
                QPointF(centre.x() + 5.0, centre.y() + 1.0),
            )
        else:
            painter.drawLine(
                QPointF(centre.x() - 4.5, centre.y() - 4.5),
                QPointF(centre.x() + 4.5, centre.y() + 4.5),
            )
            painter.drawLine(
                QPointF(centre.x() + 4.5, centre.y() - 4.5),
                QPointF(centre.x() - 4.5, centre.y() + 4.5),
            )


class _TitleBar(QFrame):
    """Drag handle for the frameless panel.

    Wayland compositors ignore ``QWidget.move()`` for toplevel windows, so the
    press asks the compositor to start an interactive move. A manual fallback
    keeps dragging working on X11 too.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(38)
        self._drag_offset: QPoint | None = None

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            handle = window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag_offset = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class _ChatView(QScrollArea):
    """Scrollable, chat-style conversation with one bubble per message."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setObjectName("ChatView")

        self._body = QWidget()
        self._body.setObjectName("ChatBody")
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(14, 12, 14, 14)
        self._layout.setSpacing(9)
        self._bubbles: list[tuple[QFrame, QLabel, str]] = []
        self._stream_row: QWidget | None = None
        self._stream_bubble: QFrame | None = None
        self._stream_label: QLabel | None = None
        self._stream_text = ""

        self._empty = QLabel(
            "Ask Clicky anything about your screen.\n"
            "Your conversation will appear here."
        )
        self._empty.setObjectName("Tertiary")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._layout.addWidget(self._empty)
        self._layout.addStretch(1)

        self.setWidget(self._body)
        # The scroll range is recomputed after the new bubble is laid out, so
        # follow it instead of scrolling once (which would happen too early).
        self.verticalScrollBar().rangeChanged.connect(self._on_range_changed)

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        self.verticalScrollBar().setValue(maximum)

    # -- messages ------------------------------------------------------
    def _create_bubble(self, role: str) -> tuple[QWidget, QFrame, QLabel]:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)

        bubble = QFrame()
        bubble.setObjectName("ChatBubble")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(11, 8, 11, 8)
        bubble_layout.setSpacing(0)
        label = QLabel("")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble_layout.addWidget(label)

        if role == "user":
            bubble.setStyleSheet(
                f"QFrame#ChatBubble {{ background: {theme.COLORS['accent']};"
                " border-radius: 10px; }"
                " QLabel { color: white; }"
            )
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        elif role == "error":
            bubble.setStyleSheet(
                "QFrame#ChatBubble { background: transparent;"
                f" border: 1px solid {theme.COLORS['warning']}; border-radius: 10px; }}"
                f" QLabel {{ color: {theme.COLORS['warning']}; }}"
            )
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        else:
            bubble.setStyleSheet(
                f"QFrame#ChatBubble {{ background: {theme.COLORS['surface2']};"
                f" border: 1px solid {theme.COLORS['border_subtle']}; border-radius: 10px; }}"
                f" QLabel {{ color: {theme.COLORS['text_primary']}; }}"
            )
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        return row, bubble, label

    def add_message(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._empty.hide()
        row, bubble, label = self._create_bubble(role)
        label.setText(text)
        self._bubbles.append((bubble, label, text))
        self._fit_bubble(bubble, label, text)
        self._layout.insertWidget(self._layout.count() - 1, row)
        QTimer.singleShot(0, self.scroll_to_bottom)

    # -- streaming ------------------------------------------------------
    def begin_stream(self) -> None:
        if self._stream_label is not None:
            return
        self._empty.hide()
        row, bubble, label = self._create_bubble("clicky")
        label.setText("…")
        self._stream_row = row
        self._stream_bubble = bubble
        self._stream_label = label
        self._stream_text = ""
        self._bubbles.append((bubble, label, ""))
        self._layout.insertWidget(self._layout.count() - 1, row)
        QTimer.singleShot(0, self.scroll_to_bottom)

    def append_stream(self, text: str) -> None:
        if not text:
            return
        self.begin_stream()
        self._stream_text += text
        self._render_stream()

    def _render_stream(self) -> None:
        if self._stream_label is None:
            return
        bracket = self._stream_text.find("[")
        visible = (self._stream_text[:bracket] if bracket != -1 else self._stream_text).strip() or "…"
        self._stream_label.setText(visible)
        if self._stream_bubble is not None:
            self._fit_bubble(self._stream_bubble, self._stream_label, visible)

    def end_stream(self, final_text: str | None = None) -> None:
        """Finish the live bubble, replacing it with the final spoken text."""
        if self._stream_label is None:
            if final_text and final_text.strip():
                self.add_message("clicky", final_text)
            return
        source = self._stream_text if final_text is None else final_text
        text = source.split("[POINT", 1)[0].strip()
        label = self._stream_label
        bubble = self._stream_bubble
        row = self._stream_row
        self._stream_label = None
        self._stream_bubble = None
        self._stream_row = None
        self._stream_text = ""

        if not text:
            for index, (_candidate, candidate_label, _old) in enumerate(self._bubbles):
                if candidate_label is label:
                    self._bubbles.pop(index)
                    break
            if row is not None:
                self._layout.removeWidget(row)
                row.deleteLater()
            return

        label.setText(text)
        if bubble is not None:
            self._fit_bubble(bubble, label, text)
        for index, (candidate, candidate_label, _old) in enumerate(self._bubbles):
            if candidate_label is label:
                self._bubbles[index] = (candidate, candidate_label, text)
                break

    def clear_messages(self) -> None:
        for bubble, _label, _text in self._bubbles:
            row = bubble.parentWidget()
            if row is not None:
                self._layout.removeWidget(row)
                row.deleteLater()
        self._bubbles.clear()
        self._stream_row = None
        self._stream_bubble = None
        self._stream_label = None
        self._stream_text = ""
        self._empty.show()

    def scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _bubble_width(self) -> int:
        return max(200, int(self.viewport().width() * 0.78))

    def _fit_bubble(self, bubble: QFrame, label: QLabel, text: str) -> None:
        """Size a bubble to its text, capped at a readable chat width.

        QLabel's word-wrapped size hint is unreliable, so the width is computed
        from the font metrics and the bubble is given a fixed width; Qt then
        wraps the text and derives the height.
        """
        metrics = QFontMetrics(label.font())
        lines = text.splitlines() or [text]
        natural = max(metrics.horizontalAdvance(line) for line in lines) + 26
        bubble.setFixedWidth(min(self._bubble_width(), max(90, natural)))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        for bubble, label, text in self._bubbles:
            self._fit_bubble(bubble, label, text)


class Panel(QWidget):
    """The main companion panel."""

    health_ready = pyqtSignal(object)
    config_changed = pyqtSignal()
    capture_result = pyqtSignal(bool, str)

    def __init__(
        self,
        config: Config,
        pipeline: Pipeline,
        *,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(None)
        self.config = config
        self.pipeline = pipeline
        self._on_quit = on_quit
        self._health_thread: threading.Thread | None = None
        self._health_generation = 0

        self.setObjectName("PanelRoot")
        self.setWindowTitle("Clicky")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(PANEL_MIN_WIDTH, PANEL_MIN_HEIGHT)
        try:
            preferred = int(self.config.get("ui.panel_width", PANEL_WIDTH) or PANEL_WIDTH)
        except (TypeError, ValueError):
            preferred = PANEL_WIDTH
        if preferred < PANEL_MIN_WIDTH:
            preferred = PANEL_WIDTH
        self.resize(preferred, PANEL_HEIGHT)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._card = QFrame(self)
        self._card.setObjectName("Panel")
        outer.addWidget(self._card)

        self._layout = QVBoxLayout(self._card)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._build_header()
        self._build_body()
        self._point_marker = _PointMarker(self)

        self.health_ready.connect(self._apply_health)
        self.capture_result.connect(self._capture_result)

        pipeline.state_changed.connect(self._on_state)
        pipeline.status_changed.connect(self._on_status)
        pipeline.transcript_ready.connect(self._on_transcript)
        pipeline.reply_delta.connect(self._on_reply_delta)
        pipeline.reply_ready.connect(self._on_reply)
        pipeline.error.connect(self._on_error)
        pipeline.listened.connect(self._on_listened)

        self._on_state(pipeline.state)
        self._refresh_health()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        bar = _TitleBar()
        header = QHBoxLayout(bar)
        header.setContentsMargins(16, 7, 10, 5)
        header.setSpacing(8)
        self._dot = StatusDot()
        title = QLabel("Clicky")
        title.setObjectName("Title")
        self._status = QLabel("Active")
        self._status.setObjectName("Tertiary")
        minimise = _WindowButton("minimize")
        minimise.setToolTip("Minimise to the tray")
        minimise.clicked.connect(self.minimize)
        close = _WindowButton("close")
        close.setToolTip("Close the panel (the companion keeps running)")
        close.clicked.connect(self.hide)

        header.addWidget(self._dot)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._status)
        header.addSpacing(4)
        header.addWidget(minimise)
        header.addWidget(close)
        # Let a press anywhere on the title text start the window drag.
        for widget in (self._dot, title, self._status):
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._layout.addWidget(bar)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {theme.COLORS['border_subtle']};")
        self._layout.addWidget(divider)

    def _build_body(self) -> None:
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # -- left column: every control -------------------------------------
        controls = QWidget()
        controls.setObjectName("ControlsColumn")
        controls.setMinimumWidth(CONTROLS_WIDTH)
        controls.setMaximumWidth(CONTROLS_WIDTH + 60)
        left = QVBoxLayout(controls)
        left.setContentsMargins(16, 10, 16, 12)
        left.setSpacing(8)
        self._controls_layout = left

        self._hint = QLabel(self._hint_text())
        self._hint.setObjectName("Secondary")
        self._hint.setWordWrap(True)
        left.addWidget(self._hint)

        self._onboarding = QFrame()
        self._onboarding.setObjectName("OnboardingCard")
        self._onboarding.setStyleSheet(
            f"QFrame#OnboardingCard {{ background: {theme.COLORS['surface2']}; "
            f"border: 1px solid {theme.COLORS['border_subtle']}; border-radius: 8px; }}"
        )
        onboarding_layout = QVBoxLayout(self._onboarding)
        onboarding_layout.setContentsMargins(10, 9, 10, 9)
        onboarding_layout.setSpacing(4)
        heading = QLabel("hey, i'm clicky")
        heading.setStyleSheet("font-weight: 600;")
        copy = QLabel(
            "i only look at your screen when you press the shortcut. "
            "pick a provider and add a key below, or run a local ollama model."
        )
        copy.setObjectName("Tertiary")
        copy.setWordWrap(True)
        got_it = QPushButton("got it")
        got_it.setObjectName("OutlineCapsule")
        got_it.setCursor(Qt.CursorShape.PointingHandCursor)
        got_it.clicked.connect(self._dismiss_onboarding)
        onboarding_layout.addWidget(heading)
        onboarding_layout.addWidget(copy)
        onboarding_layout.addWidget(got_it, 0, Qt.AlignmentFlag.AlignLeft)
        left.addWidget(self._onboarding)
        if bool(self.config.get("ui.onboarding_completed", False)):
            self._onboarding.hide()

        # -- provider + model ------------------------------------------------
        section = QLabel("MODEL")
        section.setObjectName("SectionLabel")
        left.addWidget(section)

        provider_row = QHBoxLayout()
        provider_row.setSpacing(6)
        self._provider_buttons: dict[str, QPushButton] = {}
        for provider_id in PROVIDERS:
            button = QPushButton(PROVIDER_LABELS.get(provider_id, DISPLAY_NAMES[provider_id]))
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(self._segment_style())
            button.clicked.connect(lambda _checked=False, pid=provider_id: self._choose_provider(pid))
            self._provider_buttons[provider_id] = button
            provider_row.addWidget(button, 1)
        left.addLayout(provider_row)

        self._model_combo = QComboBox()
        self._model_combo.setEditable(False)
        self._model_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_combo.activated.connect(self._on_model_selected)
        left.addWidget(self._model_combo)

        # -- health ----------------------------------------------------------
        section = QLabel("SYSTEM")
        section.setObjectName("SectionLabel")
        left.addWidget(section)
        self._health_rows: dict[str, HealthRow] = {}
        for key, label in (
            ("llm", "Language model"),
            ("stt", "Speech to text"),
            ("tts", "Voice"),
            ("capture", "Screen capture"),
            ("hotkey", "Shortcut"),
        ):
            row = HealthRow(label)
            self._health_rows[key] = row
            left.addWidget(row)

        self._error_banner = QLabel()
        self._error_banner.setWordWrap(True)
        self._error_banner.setObjectName("Warning")
        self._error_banner.hide()
        left.addWidget(self._error_banner)

        left.addStretch(1)

        # -- talk ------------------------------------------------------------
        self._talk = QPushButton(self._idle_talk_text())
        self._talk.setObjectName("Primary")
        self._talk.setCursor(Qt.CursorShape.PointingHandCursor)
        self._talk.setMinimumHeight(38)
        self._talk.clicked.connect(self.pipeline.toggle_listening)
        left.addWidget(self._talk)

        # -- footer ----------------------------------------------------------
        footer = QHBoxLayout()
        footer.setSpacing(6)
        self._settings_button = QPushButton("Settings")
        self._settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_button.clicked.connect(self.open_settings)
        self._capture_button = QPushButton("Test capture")
        self._capture_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._capture_button.clicked.connect(self.test_capture)
        self._quit_button = QPushButton("Quit")
        self._quit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._quit_button.clicked.connect(self._quit)
        footer.addWidget(self._settings_button)
        footer.addWidget(self._capture_button)
        footer.addStretch(1)
        footer.addWidget(self._quit_button)
        left.addLayout(footer)

        version = QLabel("heyclicky for linux · port of farzaa/clicky")
        version.setObjectName("Tertiary")
        version.setStyleSheet("font-size: 10px;")
        left.addWidget(version)

        body.addWidget(controls)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet(f"color: {theme.COLORS['border_subtle']};")
        body.addWidget(separator)

        # -- right column: chat ---------------------------------------------
        chat_column = QWidget()
        chat_column.setObjectName("ChatColumn")
        right = QVBoxLayout(chat_column)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        chat_header = QHBoxLayout()
        chat_header.setContentsMargins(16, 10, 12, 8)
        chat_header.setSpacing(8)
        chat_title = QLabel("Conversation")
        chat_title.setObjectName("ChatTitle")
        self._chat_status = QLabel("")
        self._chat_status.setObjectName("Tertiary")
        self._clear_chat = QPushButton("Clear")
        self._clear_chat.setObjectName("OutlineCapsule")
        self._clear_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_chat.setToolTip("Clear the conversation")
        self._clear_chat.clicked.connect(self._clear_conversation)
        chat_header.addWidget(chat_title)
        chat_header.addWidget(self._chat_status)
        chat_header.addStretch(1)
        chat_header.addWidget(self._clear_chat)
        right.addLayout(chat_header)

        chat_divider = QFrame()
        chat_divider.setFrameShape(QFrame.Shape.HLine)
        chat_divider.setStyleSheet(f"color: {theme.COLORS['border_subtle']};")
        right.addWidget(chat_divider)

        self._chat = _ChatView()
        right.addWidget(self._chat, 1)

        body.addWidget(chat_column, 1)
        self._layout.addLayout(body, 1)

        self._sync_provider_buttons()

    def minimize(self) -> None:
        """Hide the panel; the tray icon and shortcuts keep the app alive."""
        self.hide()

    def _clear_conversation(self) -> None:
        self._chat.clear_messages()
        self._chat_status.setText("")

    @staticmethod
    def _segment_style() -> str:
        return f"""
        QPushButton {{
            background: rgba(255, 255, 255, 10);
            border: 1px solid {theme.COLORS['border_subtle']};
            border-radius: 6px;
            padding: 5px 8px;
            font-size: 11px;
            color: {theme.COLORS['text_tertiary']};
        }}
        QPushButton:checked {{
            background: rgba(255, 255, 255, 26);
            color: {theme.COLORS['text_primary']};
        }}
        """

    def _hint_text(self) -> str:
        hint = self.pipeline.shortcut_hint()
        return (hint[:1].upper() + hint[1:] + ".") if hint else ""

    def _idle_talk_text(self) -> str:
        if getattr(self.pipeline.hotkey, "toggle_mode", False):
            return "Start listening"
        return "Hold shortcut to talk"

    # ------------------------------------------------------------------
    # Visibility / placement
    # ------------------------------------------------------------------
    def show_near_cursor(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(max(self.width(), PANEL_MIN_WIDTH), available.width() - 24)
            height = min(max(self.height(), PANEL_MIN_HEIGHT), available.height() - 24)
            self.resize(width, height)
            x = available.right() - width - 16
            y = available.top() + 24
            self.move(max(available.left(), x), max(available.top(), y))
        self.show()
        self.raise_()
        self.activateWindow()
        self._refresh_health()

    def show_point_marker(self, global_x: float, global_y: float, label: str = "") -> bool:
        """Point at something inside the panel, on top of the panel's widgets.

        Returns ``True`` when the target was aimed at the panel. The
        screen-wide overlay cannot draw above this always-on-top window on
        Wayland, so the panel renders the ring itself.

        Wayland also hides the panel's true position from Qt (the compositor,
        not the client, places toplevels), which makes ``mapFromGlobal``
        unreliable. When the model names one of our own controls we therefore
        point at that widget directly.
        """
        if not self.isVisible():
            return False

        widget = self._panel_widget_for_label(label)
        if widget is not None:
            center = widget.mapTo(self, widget.rect().center())
            self._point_marker.show_at(center.x(), center.y(), label)
            return True

        local = self.mapFromGlobal(QPoint(int(global_x), int(global_y)))
        if not self.rect().contains(local):
            return False
        self._point_marker.show_at(local.x(), local.y(), label)
        return True

    def _panel_widget_for_label(self, label: str) -> QWidget | None:
        """Match a model-provided element label to one of the panel's widgets."""
        text = (label or "").lower()
        if not text:
            return None
        mentions_panel = any(
            word in text for word in ("clicky", "heyclicky")
        )
        if "test capture" in text or (mentions_panel and "capture" in text):
            return self._capture_button
        if mentions_panel and "settings" in text:
            return self._settings_button
        if mentions_panel and "quit" in text:
            return self._quit_button
        if (
            "listen" in text
            or "talk button" in text
            or mentions_panel
        ):
            return self._talk
        return None

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_near_cursor()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Pipeline events
    # ------------------------------------------------------------------
    def _on_state(self, state: VoiceState) -> None:
        visible = bool(self.pipeline.overlay.visible)
        self._dot.set_status(state, visible)
        label = {VoiceState.IDLE: "Active", VoiceState.LISTENING: "Listening",
                 VoiceState.PROCESSING: "Processing", VoiceState.RESPONDING: "Responding"}[state]
        self._status.setText(label if visible else "Ready")
        if state is VoiceState.LISTENING:
            self._talk.setText("Stop and send")
            self._chat_status.setText("listening…")
        elif state is VoiceState.PROCESSING:
            self._talk.setText(self._idle_talk_text())
            self._chat_status.setText("thinking…")
        elif state is VoiceState.RESPONDING:
            self._talk.setText(self._idle_talk_text())
            self._chat_status.setText("answering…")
        else:
            self._talk.setText(self._idle_talk_text())
            self._chat_status.setText("")

    def _on_status(self, text: str) -> None:
        if not text:
            return
        key = text.rstrip("….").strip().lower()
        # Pipeline status also carries the shortcut hint and the active model,
        # which belong in the controls column, not the chat header.
        if key in {"listening", "thinking", "speaking", "transcribing", "capturing"}:
            self._status.setText(key.capitalize())
            self._chat_status.setText(f"{key}…")

    def _on_listened(self, listening: bool) -> None:
        self._talk.setText("Stop and send" if listening else self._idle_talk_text())

    def _on_transcript(self, text: str) -> None:
        self._error_banner.hide()
        self._chat.end_stream()
        self._chat.add_message("user", text)

    def _on_reply_delta(self, text: str) -> None:
        self._chat.append_stream(text)

    def _on_reply(self, text: str) -> None:
        self._chat.end_stream(text or "")

    def _on_error(self, message: str) -> None:
        self._chat.end_stream()
        short = message if len(message) < 240 else message[:237] + "…"
        self._error_banner.setText(short)
        self._error_banner.show()
        self._chat.add_message("error", short)
        self._refresh_health()

    def _dismiss_onboarding(self) -> None:
        self.config.set("ui.onboarding_completed", True)
        self.config.save()
        self._onboarding.hide()
        self.pipeline.show_welcome()

    # ------------------------------------------------------------------
    # Provider + model
    # ------------------------------------------------------------------
    def _sync_provider_buttons(self) -> None:
        current = str(self.config.get("llm.provider", "groq")).lower()
        for provider_id, button in self._provider_buttons.items():
            button.setChecked(provider_id == current)
        model = str(self.config.get(f"llm.{current}.model", DEFAULT_MODELS.get(current, "")))
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.addItem(model)
        self._model_combo.setCurrentText(model)
        self._model_combo.blockSignals(False)

    def _choose_provider(self, provider_id: str) -> None:
        if provider_id == str(self.config.get("llm.provider", "")):
            return
        self.pipeline.set_provider(provider_id)
        self._sync_provider_buttons()
        self._refresh_health()
        self.config_changed.emit()

    def _on_model_selected(self, _index: int) -> None:
        model = self._model_combo.currentText().strip()
        provider = str(self.config.get("llm.provider", "groq"))
        if model and model != self.config.get(f"llm.{provider}.model"):
            self.pipeline.set_provider(provider, model)
            self._refresh_health()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def _refresh_health(self) -> None:
        self._health_generation += 1
        generation = self._health_generation

        def work() -> None:
            result = _collect_health(self.config, self.pipeline)
            if generation == self._health_generation:
                self.health_ready.emit(result)

        self._health_thread = threading.Thread(
            target=work, name="heyclicky-health", daemon=True
        )
        self._health_thread.start()

    def _apply_health(self, rows: list[dict]) -> None:
        models: list[str] = []
        for row in rows:
            widget = self._health_rows.get(row["key"])
            if widget is not None:
                widget.set_health(bool(row["ok"]), str(row["detail"]))
            if row["key"] == "llm":
                models = list(row.get("models") or [])
        if models:
            current = str(self.config.get(f"llm.{self.config.get('llm.provider', 'groq')}.model", ""))
            ordered = [current] + [model for model in models if model != current]
            self._model_combo.blockSignals(True)
            self._model_combo.clear()
            self._model_combo.addItems(ordered)
            self._model_combo.setCurrentText(current)
            self._model_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.pipeline.reload_engines()
            self.pipeline.restart_hotkey()
            self._hint.setText(self._hint_text())
            self._sync_provider_buttons()
            self._refresh_health()
            self.config_changed.emit()
            self.pipeline.overlay.set_edge_glow(bool(self.config.get("ui.edge_glow", True)))

    def test_capture(self) -> None:
        self._talk.setEnabled(False)
        self._talk.setText("Capturing…")
        # Hide the glow so it doesn't end up in the screenshot.
        self.pipeline.overlay.hide_for_capture()

        def work() -> None:
            try:
                bundle = self.pipeline.capture_once()
                path = "/tmp/heyclicky-capture.jpg"
                with open(path, "wb") as handle:
                    handle.write(bundle.images[0].data)
                self.capture_result.emit(
                    True,
                    f"captured {len(bundle.images)} screen(s) with {bundle.backend}.\nsaved {path}",
                )
            except (CaptureError, OSError) as exc:
                self.capture_result.emit(False, str(exc))

        threading.Thread(target=work, name="heyclicky-capture-test", daemon=True).start()

    def _capture_result(self, ok: bool, message: str) -> None:
        self.pipeline.overlay.show_after_capture()
        self._talk.setEnabled(True)
        self._talk.setText(self._idle_talk_text())
        box = QMessageBox(self)
        box.setWindowTitle("Screen capture")
        box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning)
        box.setText(message)
        box.exec()

    def _quit(self) -> None:
        self.hide()
        if self._on_quit is not None:
            self._on_quit()


def _collect_health(config: Config, pipeline: Pipeline) -> list[dict]:
    rows: list[dict] = []
    provider_id = str(config.get("llm.provider", "groq")).lower()
    models: list[str] = []
    try:
        provider = make_provider(config, provider_id)
        ok, detail = provider.health()
        models = provider.list_models() if ok else []
    except Exception as exc:
        ok, detail = False, str(exc)
    rows.append({"key": "llm", "ok": ok, "detail": detail, "models": models})

    try:
        stt = make_stt(config)
        ok, detail = stt.health()
        detail = f"{stt.display_name}: {detail}"
    except Exception as exc:
        ok, detail = False, str(exc)
    rows.append({"key": "stt", "ok": ok, "detail": detail})

    try:
        ok, detail = tts_available(config)
        if not bool(config.get("tts.enabled", True)):
            detail = f"{detail} (muted)"
        if not shutil.which("ffplay") and not shutil.which("paplay") and not shutil.which("aplay"):
            ok, detail = False, "no audio player (install ffmpeg)"
    except Exception as exc:
        ok, detail = False, str(exc)
    rows.append({"key": "tts", "ok": ok, "detail": detail})

    try:
        detail = pipeline.capture.backend_name()
        ok = not detail.startswith("unavailable")
    except Exception as exc:
        ok, detail = False, str(exc)
    rows.append({"key": "capture", "ok": ok, "detail": detail})

    try:
        ok = True
        detail = pipeline.hotkey.detail()
    except Exception as exc:
        ok, detail = False, str(exc)
    rows.append({"key": "hotkey", "ok": ok, "detail": detail})
    return rows


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsDialog(QDialog):
    voice_test_result = pyqtSignal(str)

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._voice_test_thread: threading.Thread | None = None
        self.setWindowTitle("Clicky settings")
        self.setMinimumWidth(480)
        self.setModal(True)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._models_tab(), "Models")
        tabs.addTab(self._voice_tab(), "Voice")
        tabs.addTab(self._shortcuts_tab(), "Shortcuts & capture")
        tabs.addTab(self._privacy_tab(), "Privacy")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.voice_test_result.connect(self._on_voice_test_result)
        self._load()

    # -- tabs ----------------------------------------------------------
    def _models_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.provider_combo = QComboBox()
        for provider_id in PROVIDERS:
            self.provider_combo.addItem(DISPLAY_NAMES[provider_id], provider_id)
        form.addRow("Default provider", self.provider_combo)
        layout.addLayout(form)

        self.reasoning_combo = QComboBox()
        for value, label in (
            ("off", "Off (fastest - recommended)"),
            ("low", "Low"),
            ("model", "Let the model decide"),
        ):
            self.reasoning_combo.addItem(label, value)
        reasoning_form = QFormLayout()
        reasoning_form.addRow("Model reasoning", self.reasoning_combo)
        reasoning_hint = QLabel(
            "Reasoning models can think for several seconds before answering, "
            "and a small token budget can even come back empty. Keep this off "
            "for snappy voice replies."
        )
        reasoning_hint.setObjectName("Tertiary")
        reasoning_hint.setWordWrap(True)
        reasoning_form.addRow("", reasoning_hint)
        layout.addLayout(reasoning_form)

        self.openrouter_key = QLineEdit()
        self.openrouter_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openrouter_key.setPlaceholderText("sk-or-… (or set OPENROUTER_GEMMA_API_KEY)")
        self.openrouter_model = QLineEdit()
        self.openrouter_url = QLineEdit()
        self.openrouter_url.setPlaceholderText("https://openrouter.ai/api/v1")
        layout.addWidget(
            self._group(
                "OpenRouter",
                [
                    ("API key", self.openrouter_key),
                    ("Model", self.openrouter_model),
                    ("Base URL", self.openrouter_url),
                ],
            )
        )

        self.groq_key = QLineEdit()
        self.groq_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key.setPlaceholderText("gsk_… (or set GROQ_API_KEY)")
        self.groq_model = QLineEdit()
        self.groq_url = QLineEdit()
        layout.addWidget(self._group("Groq", [("API key", self.groq_key), ("Model", self.groq_model), ("Base URL", self.groq_url)]))

        self.gemini_key = QLineEdit()
        self.gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key.setPlaceholderText("AIza… (or set GEMINI_API_KEY)")
        self.gemini_model = QLineEdit()
        self.gemini_url = QLineEdit()
        layout.addWidget(self._group("Gemini", [("API key", self.gemini_key), ("Model", self.gemini_model), ("Base URL", self.gemini_url)]))

        self.ollama_url = QLineEdit()
        self.ollama_model = QLineEdit()
        self.ollama_keep_alive = QLineEdit()
        layout.addWidget(
            self._group(
                "Ollama (local)",
                [("Base URL", self.ollama_url), ("Model", self.ollama_model), ("Keep alive", self.ollama_keep_alive)],
            )
        )
        helper = QLabel(
            "Screenshots are sent to Groq or Gemini only when you use the shortcut. "
            "Ollama keeps everything on this machine; qwen2.5vl:7b can read images, "
            "text-only qwen2.5 answers from your words alone."
        )
        helper.setObjectName("Tertiary")
        helper.setWordWrap(True)
        layout.addWidget(helper)
        layout.addStretch(1)
        return page

    def _voice_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        stt_group = QGroupBox("Speech to text")
        stt_form = QFormLayout(stt_group)
        self.stt_combo = QComboBox()
        for value, label in (
            ("auto", "Auto"),
            ("groq", "Groq Whisper"),
            ("gemini", "Gemini audio"),
            ("local", "Local faster-whisper"),
        ):
            self.stt_combo.addItem(label, value)
        self.language_edit = QLineEdit()
        self.language_edit.setPlaceholderText("en")
        stt_form.addRow("Engine", self.stt_combo)
        stt_form.addRow("Language", self.language_edit)
        layout.addWidget(stt_group)

        tts_group = QGroupBox("Voice")
        tts_form = QFormLayout(tts_group)
        self.tts_enabled = QCheckBox("Speak replies aloud")
        self.tts_combo = QComboBox()
        for value, label in (
            ("auto", "Auto"),
            ("edge", "Edge neural voice"),
            ("gemini", "Gemini voice"),
            ("piper", "Piper (offline)"),
            ("speech-dispatcher", "Speech Dispatcher"),
        ):
            self.tts_combo.addItem(label, value)
        self.voice_edit = QLineEdit()
        self.voice_edit.setPlaceholderText("en-US-AriaNeural")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.voice_test = QPushButton("Test voice")
        self.voice_test.setObjectName("OutlineCapsule")
        self.voice_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_test.clicked.connect(self._test_voice)
        self.voice_test_status = QLabel("")
        self.voice_test_status.setObjectName("Tertiary")
        self.voice_test_status.setWordWrap(True)
        tts_form.addRow("", self.tts_enabled)
        tts_form.addRow("Engine", self.tts_combo)
        tts_form.addRow("Edge voice", self.voice_edit)
        tts_form.addRow("Volume", self.volume)
        tts_form.addRow("", self.voice_test)
        tts_form.addRow("", self.voice_test_status)
        layout.addWidget(tts_group)

        audio_group = QGroupBox("Microphone")
        audio_form = QFormLayout(audio_group)
        self.input_combo = QComboBox()
        for device in list_input_devices():
            self.input_combo.addItem(device.description, device.id)
        audio_form.addRow("Input device", self.input_combo)
        layout.addWidget(audio_group)
        layout.addStretch(1)
        return page

    def _shortcuts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hotkey_group = QGroupBox("Push to talk")
        hotkey_form = QFormLayout(hotkey_group)
        self.hotkey_backend = QComboBox()
        for value, label in (
            ("auto", "Auto"),
            ("evdev", "evdev (true key events, needs input group)"),
            ("pynput", "pynput (X11)"),
            ("gnome", "GNOME keybinding (adds Space, toggle)"),
            ("manual", "Panel button only"),
        ):
            self.hotkey_backend.addItem(label, value)

        self.hotkey_combo = QComboBox()
        self.hotkey_combo.setEditable(True)
        self.hotkey_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for combo in ("ctrl+alt", "ctrl+shift", "alt+shift", "super+shift", "fn+super"):
            self.hotkey_combo.addItem(hotkey_label(combo), combo)
        self.hotkey_combo.lineEdit().setPlaceholderText("ctrl+alt")

        self.hotkey_mode = QComboBox()
        for value, label in (
            ("auto", "Auto (toggle for Fn chords, hold otherwise)"),
            ("toggle", "Press once to start, again to stop"),
            ("hold", "Hold to talk, release to send"),
        ):
            self.hotkey_mode.addItem(label, value)

        self.fn_check = QPushButton("Check Fn key")
        self.fn_check.clicked.connect(self._check_fn_key)

        hotkey_form.addRow("Backend", self.hotkey_backend)
        hotkey_form.addRow("Shortcut", self.hotkey_combo)
        hotkey_form.addRow("Mode", self.hotkey_mode)
        hotkey_form.addRow("", self.fn_check)
        hotkey_hint = QLabel(
            "Ctrl+Alt+Space is the recommended default. GNOME adds Space and "
            "uses press-once-to-start, press-again-to-stop. Avoid Super+Space "
            "(input-source switch). Fn usually never reaches Linux."
        )
        hotkey_hint.setObjectName("Tertiary")
        hotkey_hint.setWordWrap(True)
        hotkey_form.addRow("", hotkey_hint)
        layout.addWidget(hotkey_group)

        capture_group = QGroupBox("Screen capture")
        capture_form = QFormLayout(capture_group)
        self.capture_backend = QComboBox()
        for value in ("auto", "screencast", "portal", "x11", "gnome", "cli"):
            self.capture_backend.addItem(value, value)
        self.max_dimension = QSpinBox()
        self.max_dimension.setRange(512, 2048)
        self.max_dimension.setSingleStep(64)
        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(50, 95)
        self.hide_overlay = QCheckBox("Hide the overlay while taking a screenshot")
        capture_form.addRow("Backend", self.capture_backend)
        capture_form.addRow("Max dimension", self.max_dimension)
        capture_form.addRow("JPEG quality", self.jpeg_quality)
        capture_form.addRow("", self.hide_overlay)
        layout.addWidget(capture_group)

        ui_group = QGroupBox("Overlay")
        ui_layout = QVBoxLayout(ui_group)
        self.edge_glow = QCheckBox("Glow around the screen edges while listening")
        self.start_minimized = QCheckBox("Start minimized (tray icon only)")
        self.transient_hide = QSpinBox()
        self.transient_hide.setRange(0, 10)
        self.transient_hide.setSuffix(" s")
        row = QFormLayout()
        row.addRow("Hide after", self.transient_hide)
        ui_layout.addWidget(self.edge_glow)
        ui_layout.addWidget(self.start_minimized)
        ui_layout.addLayout(row)
        layout.addWidget(ui_group)
        layout.addStretch(1)
        return page

    def _privacy_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.store_history = QCheckBox("Keep conversation history on disk (not implemented yet)")
        self.store_history.setEnabled(False)
        layout.addWidget(self.store_history)
        note = QLabel(
            "heyclicky does not run background screen capture. A screenshot is taken "
            "only when a transcription is ready, then it is sent to the provider you "
            "selected. Keys live in "
            f"{self.config.path or '~/.config/heyclicky/config.json'} with mode 0600. "
            "Use Ollama for a fully local setup."
        )
        note.setWordWrap(True)
        note.setObjectName("Tertiary")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    @staticmethod
    def _group(title: str, fields: list[tuple[str, QWidget]]) -> QGroupBox:
        group = QGroupBox(title)
        form = QFormLayout(group)
        for label, widget in fields:
            form.addRow(label, widget)
        return group

    # -- load / save ---------------------------------------------------
    def _check_fn_key(self) -> None:
        supported, detail = fn_key_supported()
        if supported:
            QMessageBox.information(
                self,
                "Fn key",
                f"This keyboard reports the Fn key ({detail}).\n\n"
                "An Fn chord can be used with the evdev backend. You may need to "
                "add yourself to the input group:\n\n"
                "sudo usermod -aG input $USER\n"
                "(then log out and back in)",
            )
        else:
            QMessageBox.warning(
                self,
                "Fn key",
                f"The Fn key is not available: {detail}.\n\n"
                "On most laptops the Fn key is consumed by the embedded controller, "
                "so no Linux application can see it. Pressing Fn+Super only sends "
                "Super to the system.\n\n"
                "Pick another shortcut instead, such as Ctrl+Alt (the default) "
                "or Ctrl+Shift.",
            )

    def _load(self) -> None:
        current = str(self.config.get("llm.provider", "openrouter"))
        index = self.provider_combo.findData(current)
        self.provider_combo.setCurrentIndex(max(0, index))
        self.openrouter_key.setText(str(self.config.get("llm.openrouter.api_key", "")))
        self.openrouter_model.setText(str(self.config.get("llm.openrouter.model", "")))
        self.openrouter_url.setText(str(self.config.get("llm.openrouter.base_url", "")))
        self.groq_key.setText(str(self.config.get("llm.groq.api_key", "")))
        self.groq_model.setText(str(self.config.get("llm.groq.model", "")))
        self.groq_url.setText(str(self.config.get("llm.groq.base_url", "")))
        self.gemini_key.setText(str(self.config.get("llm.gemini.api_key", "")))
        self.gemini_model.setText(str(self.config.get("llm.gemini.model", "")))
        self.gemini_url.setText(str(self.config.get("llm.gemini.base_url", "")))
        self.ollama_url.setText(str(self.config.get("llm.ollama.base_url", "")))
        self.ollama_model.setText(str(self.config.get("llm.ollama.model", "")))
        self.ollama_keep_alive.setText(str(self.config.get("llm.ollama.keep_alive", "10m")))
        reasoning = str(self.config.get("llm.reasoning", "off"))
        self.reasoning_combo.setCurrentIndex(max(0, self.reasoning_combo.findData(reasoning)))

        stt = str(self.config.get("stt.provider", "auto"))
        self.stt_combo.setCurrentIndex(max(0, self.stt_combo.findData(stt)))
        self.language_edit.setText(str(self.config.get("stt.language", "en")))
        self.tts_enabled.setChecked(bool(self.config.get("tts.enabled", True)))
        tts = str(self.config.get("tts.provider", "auto"))
        self.tts_combo.setCurrentIndex(max(0, self.tts_combo.findData(tts)))
        self.voice_edit.setText(str(self.config.get("tts.voice", "en-US-AriaNeural")))
        self.volume.setValue(int(float(self.config.get("tts.volume", 0.9)) * 100))
        device = str(self.config.get("audio.input_device", "default"))
        self.input_combo.setCurrentIndex(max(0, self.input_combo.findData(device)))

        backend = str(self.config.get("hotkey.backend", "auto"))
        self.hotkey_backend.setCurrentIndex(max(0, self.hotkey_backend.findData(backend)))
        combo = str(self.config.get("hotkey.combo", "ctrl+alt"))
        index = self.hotkey_combo.findData(combo)
        if index >= 0:
            self.hotkey_combo.setCurrentIndex(index)
        else:
            self.hotkey_combo.setEditText(combo)
        mode = str(self.config.get("hotkey.mode", "auto"))
        self.hotkey_mode.setCurrentIndex(max(0, self.hotkey_mode.findData(mode)))
        capture = str(self.config.get("capture.backend", "auto"))
        self.capture_backend.setCurrentIndex(max(0, self.capture_backend.findData(capture)))
        self.max_dimension.setValue(int(self.config.get("capture.max_dimension", 1280)))
        self.jpeg_quality.setValue(int(self.config.get("capture.jpeg_quality", 80)))
        self.hide_overlay.setChecked(bool(self.config.get("capture.hide_overlay", True)))
        self.edge_glow.setChecked(bool(self.config.get("ui.edge_glow", True)))
        self.start_minimized.setChecked(bool(self.config.get("ui.start_minimized", False)))
        self.transient_hide.setValue(int(float(self.config.get("ui.transient_hide_seconds", 1.0))))

    def _test_voice(self) -> None:
        """Synthesise a sample with the *current* form values, without saving."""
        if self._voice_test_thread is not None and self._voice_test_thread.is_alive():
            return
        trial = Config(data=copy.deepcopy(self.config.data), path=None)
        trial.set("tts.provider", self.tts_combo.currentData())
        trial.set("tts.voice", self.voice_edit.text().strip() or "en-US-AriaNeural")
        trial.set("tts.volume", self.volume.value() / 100.0)
        trial.set("tts.enabled", True)
        self.voice_test.setEnabled(False)
        self.voice_test_status.setText("synthesising…")

        def work() -> None:
            cancel = threading.Event()
            try:
                engine = make_tts(trial)
                engine.speak("hey, this is clicky. my voice is working.", cancel=cancel)
                message = "played through your default audio output"
            except Exception as exc:
                message = f"voice failed: {exc}"
            self.voice_test_result.emit(message)

        self._voice_test_thread = threading.Thread(
            target=work, name="heyclicky-voice-test", daemon=True
        )
        self._voice_test_thread.start()

    def _on_voice_test_result(self, message: str) -> None:
        self.voice_test.setEnabled(True)
        self.voice_test_status.setText(message)

    def _save(self) -> None:
        self.config.set("llm.provider", self.provider_combo.currentData())
        self.config.set("llm.reasoning", self.reasoning_combo.currentData())
        self.config.set("llm.openrouter.api_key", self.openrouter_key.text().strip())
        self.config.set("llm.openrouter.model", self.openrouter_model.text().strip())
        self.config.set("llm.openrouter.base_url", self.openrouter_url.text().strip())
        self.config.set("llm.groq.api_key", self.groq_key.text().strip())
        self.config.set("llm.groq.model", self.groq_model.text().strip())
        self.config.set("llm.groq.base_url", self.groq_url.text().strip())
        self.config.set("llm.gemini.api_key", self.gemini_key.text().strip())
        self.config.set("llm.gemini.model", self.gemini_model.text().strip())
        self.config.set("llm.gemini.base_url", self.gemini_url.text().strip())
        self.config.set("llm.ollama.base_url", self.ollama_url.text().strip())
        self.config.set("llm.ollama.model", self.ollama_model.text().strip())
        self.config.set("llm.ollama.keep_alive", self.ollama_keep_alive.text().strip() or "10m")

        self.config.set("stt.provider", self.stt_combo.currentData())
        self.config.set("stt.language", self.language_edit.text().strip() or "en")
        self.config.set("tts.enabled", self.tts_enabled.isChecked())
        self.config.set("tts.provider", self.tts_combo.currentData())
        self.config.set("tts.voice", self.voice_edit.text().strip() or "en-US-AriaNeural")
        self.config.set("tts.volume", self.volume.value() / 100.0)
        self.config.set("audio.input_device", self.input_combo.currentData() or "default")

        self.config.set("hotkey.backend", self.hotkey_backend.currentData())
        combo = self.hotkey_combo.currentText().strip().lower().replace(" ", "")
        if combo.endswith("+space"):
            # GNOME appends Space itself and evdev backends listen for a
            # modifier chord, so store just the modifiers.
            combo = combo[: -len("+space")]
        self.config.set("hotkey.combo", combo or "ctrl+alt")
        self.config.set("hotkey.mode", self.hotkey_mode.currentData())
        self.config.set("capture.backend", self.capture_backend.currentData())
        self.config.set("capture.max_dimension", self.max_dimension.value())
        self.config.set("capture.jpeg_quality", self.jpeg_quality.value())
        self.config.set("capture.hide_overlay", self.hide_overlay.isChecked())
        self.config.set("ui.edge_glow", self.edge_glow.isChecked())
        self.config.set("ui.start_minimized", self.start_minimized.isChecked())
        self.config.set("ui.transient_hide_seconds", float(self.transient_hide.value()))

        self.config.save()
        self.accept()
