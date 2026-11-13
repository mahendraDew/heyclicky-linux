"""System tray icon and menu.

GNOME only shows legacy tray icons with the AppIndicator extension installed,
so the panel can also be opened with ``heyclicky --show-panel`` or the global
shortcut. The tray is still the closest match to the macOS menu-bar item and
works out of the box on KDE, XFCE, Cinnamon and Ubuntu with AppIndicator.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QPolygonF
from PyQt6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

from ..pipeline import Pipeline
from ..state import VoiceState
from ..ui import theme


def app_icon(size: int = 64, state: VoiceState | None = None) -> QIcon:
    """The Clicky mark (a stylised pointer), drawn at any size.

    When ``state`` is given a small status dot is added so the tray icon shows
    whether Clicky is idle, listening or busy without opening the menu.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    center = QPointF(size / 2, size / 2)
    height = size * 0.62
    triangle = QPolygonF(
        [
            QPointF(center.x(), center.y() - height * 0.62),
            QPointF(center.x() - height * 0.52, center.y() + height * 0.38),
            QPointF(center.x() + height * 0.52, center.y() + height * 0.38),
        ]
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.COLORS["overlay_cursor_blue"]))
    painter.drawPolygon(triangle)

    if state is not None:
        colours = {
            VoiceState.IDLE: theme.COLORS["success"],
            VoiceState.LISTENING: theme.COLORS["accent_text"],
            VoiceState.PROCESSING: theme.COLORS["warning"],
            VoiceState.RESPONDING: theme.COLORS["accent_text"],
        }
        dot = size * 0.34
        x = size - dot - size * 0.04
        y = size - dot - size * 0.04
        painter.setBrush(QColor(theme.COLORS["background"]))
        painter.drawEllipse(QRectF(x - 1.5, y - 1.5, dot + 3, dot + 3))
        painter.setBrush(QColor(colours.get(state, theme.COLORS["success"])))
        painter.drawEllipse(QRectF(x, y, dot, dot))
    painter.end()
    return QIcon(pixmap)


class Tray(QObject):
    panel_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    capture_requested = pyqtSignal()

    def __init__(self, pipeline: Pipeline, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pipeline = pipeline
        self.icon = QSystemTrayIcon(app_icon(state=VoiceState.IDLE), self)
        self.icon.setToolTip("Clicky — ready. Click to open the panel.")
        self.menu = QMenu()
        self._build_menu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        pipeline.state_changed.connect(self._on_state)
        pipeline.listened.connect(self._on_listened)
        self._on_state(pipeline.state)

    def _build_menu(self) -> None:
        self.status_action = QAction("● Ready", self.menu)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)

        self.menu.addSeparator()

        show = QAction("Open Clicky panel", self.menu)
        show.triggered.connect(self.panel_requested.emit)
        self.menu.addAction(show)

        self.talk_action = QAction("Start listening", self.menu)
        self.talk_action.triggered.connect(self.pipeline.toggle_listening)
        self.menu.addAction(self.talk_action)

        self.menu.addSeparator()

        self.glow_action = QAction("Edge glow while active", self.menu)
        self.glow_action.setCheckable(True)
        self.glow_action.setChecked(bool(self.pipeline.config.get("ui.edge_glow", True)))
        self.glow_action.toggled.connect(self._toggle_glow)
        self.menu.addAction(self.glow_action)

        settings = QAction("Settings…", self.menu)
        settings.triggered.connect(self.settings_requested.emit)
        self.menu.addAction(settings)

        capture = QAction("Test screen capture", self.menu)
        capture.triggered.connect(self.capture_requested.emit)
        self.menu.addAction(capture)

        self.menu.addSeparator()

        about = QAction("About heyclicky", self.menu)
        about.triggered.connect(self.show_about)
        self.menu.addAction(about)

        quit_action = QAction("Quit Clicky", self.menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        self.menu.addAction(quit_action)

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.panel_requested.emit()

    def _on_state(self, state: VoiceState) -> None:
        self.icon.setIcon(app_icon(state=state))
        if state is VoiceState.LISTENING:
            self.talk_action.setText("Stop and send")
            self.status_action.setText("● Listening")
            self.icon.setToolTip("Clicky is listening…")
        elif state is VoiceState.PROCESSING:
            self.status_action.setText("● Thinking")
            self.icon.setToolTip("Clicky is thinking…")
        elif state is VoiceState.RESPONDING:
            self.status_action.setText("● Responding")
            self.icon.setToolTip("Clicky is answering…")
        else:
            self.talk_action.setText("Start listening")
            self.status_action.setText("● Ready")
            self.icon.setToolTip("Clicky — ready. Click to open the panel.")

    def _on_listened(self, listening: bool) -> None:
        self.talk_action.setText("Stop and send" if listening else "Start listening")

    def _toggle_glow(self, enabled: bool) -> None:
        self.pipeline.config.set("ui.edge_glow", enabled)
        self.pipeline.config.save()
        self.pipeline.overlay.set_edge_glow(enabled)

    def show_about(self) -> None:
        box = QMessageBox()
        box.setWindowTitle("About heyclicky")
        box.setIconPixmap(app_icon(64).pixmap(64, 64))
        box.setText(
            "<b>heyclicky for linux</b><br>"
            "a native Linux port of <a href='https://github.com/farzaa/clicky'>farzaa/clicky</a>.<br><br>"
            "push-to-talk voice companion with screen awareness, a screen-edge "
            "glow while listening, "
            "Groq, Gemini and local Ollama support."
        )
        box.setInformativeText("Original app © Farza. MIT licensed. This port is unofficial.")
        box.exec()
