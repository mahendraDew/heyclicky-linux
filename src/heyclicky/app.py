"""Application entry point: Qt setup, single instance IPC, doctor and CLI.

Run ``heyclicky`` for the tray app, ``heyclicky --doctor`` for diagnostics, or
``heyclicky --toggle-listen`` to control an already running instance.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from . import __version__
from .audio import list_input_devices, probe_microphone
from .capture import CaptureError, ScreenCaptureService, qt_screens
from .config import Config, config_path, load_config
from .hotkey import fn_key_supported, resolve_hotkey_backend, watch_keys
from .llm.factory import PROVIDERS, make_provider
from .pipeline import Pipeline
from .stt.factory import make_stt
from .tts.factory import tts_available
from .ui import theme
from .ui.overlay import OverlayManager
from .ui.panel import Panel
from .ui.tray import Tray, app_icon

log = logging.getLogger(__name__)

SOCKET_PREFIX = "heyclicky"


def _socket_name() -> str:
    return f"{SOCKET_PREFIX}-{getattr(os, 'getuid', lambda: 0)()}"


# ---------------------------------------------------------------------------
# Single instance IPC
# ---------------------------------------------------------------------------


class IpcServer(QObject):
    def __init__(self, handler, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handler = handler
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)

    def listen(self) -> bool:
        name = _socket_name()
        # Only remove a stale socket after confirming nobody answers on it.
        if self.server.listen(name):
            return True
        QLocalServer.removeServer(name)
        return self.server.listen(name)

    def close(self) -> None:
        self.server.close()

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            socket.readyRead.connect(lambda sock=socket: self._read(sock))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        data = bytes(socket.readAll()).decode("utf-8", "replace")
        for line in data.splitlines():
            command = line.strip()
            if command:
                try:
                    self._handler(command)
                except Exception:
                    log.exception("IPC command failed: %s", command)
        socket.disconnectFromServer()


def send_command(command: str, timeout_ms: int = 500) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_socket_name())
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write((command + "\n").encode("utf-8"))
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heyclicky",
        description="A push-to-talk AI companion with screen awareness, for Linux.",
    )
    parser.add_argument("--version", action="version", version=f"heyclicky {__version__}")
    parser.add_argument("--doctor", action="store_true", help="check providers, audio, capture and hotkeys")
    parser.add_argument("--json", action="store_true", help="machine readable --doctor output")
    parser.add_argument("--capture-test", action="store_true", help="capture every screen to /tmp and exit")
    parser.add_argument(
        "--watch-keys",
        nargs="?",
        const=15.0,
        type=float,
        metavar="SECONDS",
        help="print key events for SECONDS (default 15); use with sudo to diagnose Fn keys",
    )
    parser.add_argument("--show-panel", action="store_true", help="open the panel of a running instance")
    parser.add_argument("--toggle-listen", action="store_true", help="start/stop listening in a running instance")
    parser.add_argument("--start-listen", action="store_true", help="begin listening in a running instance")
    parser.add_argument("--stop-listen", action="store_true", help="finish listening in a running instance")
    parser.add_argument("--settings", action="store_true", help="open settings in a running instance")
    parser.add_argument("--quit", action="store_true", help="quit a running instance")
    parser.add_argument("--provider", choices=PROVIDERS, help="override the LLM provider for this run")
    parser.add_argument("--model", help="override the model for the chosen provider")
    parser.add_argument("--no-welcome", action="store_true", help="skip the overlay greeting")
    parser.add_argument("--log-level", default=None, help="debug, info, warning or error")
    parser.add_argument("--config-dir", help="use an alternate config directory")
    return parser


def _configure_logging(config: Config, override: str | None) -> None:
    level_name = (override or config.get("privacy.log_level", "info") or "info").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def collect_doctor(config: Config) -> dict:
    report: dict = {
        "app": {"version": __version__, "python": platform.python_version(), "platform": platform.platform()},
        "session": {
            "type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
            "display": os.environ.get("DISPLAY", ""),
            "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        },
        "config": {
            "path": str(config.path or config_path()),
            "provider": config.get("llm.provider", "openrouter"),
            "groq_key": bool(config.get("llm.groq.api_key")),
            "openrouter_key": bool(config.get("llm.openrouter.api_key")),
            "gemini_key": bool(config.get("llm.gemini.api_key")),
            "ollama_url": config.get("llm.ollama.base_url", ""),
        },
        "checks": [],
        "notes": [],
    }

    def add(name: str, ok: bool | None, detail: str, hint: str = "") -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail, "hint": hint})

    # Providers
    for provider_id in PROVIDERS:
        try:
            provider = make_provider(config, provider_id)
            ok, detail = provider.health()
        except Exception as exc:
            ok, detail = False, str(exc)
        hint = ""
        if not ok:
            if provider_id == "groq":
                hint = "export GROQ_API_KEY=... or add it in Settings"
            elif provider_id == "openrouter":
                hint = "export OPENROUTER_GEMMA_API_KEY=... or add it in Settings"
            elif provider_id == "gemini":
                hint = "export GEMINI_API_KEY=... or add it in Settings"
            else:
                hint = "install ollama and run: ollama pull qwen2.5vl:7b"
        add(f"llm:{provider_id}", ok, detail, hint)

    # STT
    try:
        stt = make_stt(config)
        ok, detail = stt.health()
        add("speech-to-text", ok, f"{stt.display_name}: {detail}", "add a Groq/Gemini key or pip install faster-whisper")
    except Exception as exc:
        add("speech-to-text", False, str(exc))

    # TTS
    try:
        ok, detail = tts_available(config)
        add("text-to-speech", ok, detail, "sudo apt install ffmpeg")
    except Exception as exc:
        add("text-to-speech", False, str(exc))

    # Capture
    service = ScreenCaptureService(config)
    backend = service.backend_name()
    add(
        "screen-capture",
        not backend.startswith("unavailable"),
        backend,
        "on Wayland screenshots may prompt; log in with 'Ubuntu on Xorg' for silent capture",
    )

    # Hotkey
    try:
        backend_obj = resolve_hotkey_backend(config)
        ok, detail = backend_obj.available()
        if backend_obj.id == "manual":
            ok = False
            detail = "no global shortcut is available; use the panel button"
        summary = f"{backend_obj.display_name}: {detail}"
        if backend_obj.note:
            summary = f"{summary} ({backend_obj.note})"
        add("hotkey", ok, summary, "sudo usermod -aG input $USER, then log out and in")
    except Exception as exc:
        add("hotkey", False, str(exc))

    # Fn key diagnostics: most laptop firmware never reports it to the kernel.
    requested_combo = str(config.get("hotkey.combo", ""))
    if "fn" in requested_combo.lower():
        fn_ok, fn_detail = fn_key_supported()
        report["fn_key"] = {"supported": fn_ok, "detail": fn_detail}
        add(
            "fn-key",
            fn_ok,
            fn_detail,
            "use another combo in Settings → Shortcuts; Fn is firmware-handled on most laptops",
        )

    # Audio
    devices = list_input_devices()
    add(
        "microphone",
        bool(shutil.which("arecord") or shutil.which("parec")),
        f"{len(devices) - 1} ALSA input(s), arecord={'yes' if shutil.which('arecord') else 'no'}, "
        f"parec={'yes' if shutil.which('parec') else 'no'}",
        "sudo apt install alsa-utils",
    )

    # A muted or 15 %-volume source is the usual reason voice input fails.
    level_ok, level_detail = probe_microphone()
    add(
        "microphone-level",
        level_ok,
        level_detail,
        "raise the input volume in Settings → Sound, or unmute the microphone",
    )

    commands = {
        "ffmpeg/ffplay": bool(shutil.which("ffplay")),
        "notify-send": bool(shutil.which("notify-send")),
        "gdbus": bool(shutil.which("gdbus")),
        "spd-say": bool(shutil.which("spd-say")),
    }
    report["commands"] = commands

    session_type = str(report["session"]["type"]).lower()
    if session_type == "wayland":
        report["notes"].append(
            "Wayland: global hotkeys need the evdev backend (input group) or a GNOME "
            "custom keybinding; screen capture uses the ScreenCast portal and asks "
            "once which monitor(s) to share."
        )
    tray_available = None
    if QApplication.instance() is not None:
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    report["tray_available"] = tray_available
    if tray_available is False:
        report["notes"].append(
            "No system tray detected. Install the AppIndicator extension on GNOME or use "
            "'heyclicky --show-panel'."
        )
    return report


def print_doctor(report: dict) -> None:
    print(f"heyclicky {report['app']['version']} doctor")
    print(f"  python    {report['app']['python']}")
    print(f"  platform  {report['app']['platform']}")
    print(f"  session   {report['session']['type']} / {report['session']['desktop']}")
    print(f"  config    {report['config']['path']}")
    print(f"  provider  {report['config']['provider']}")
    print()
    for check in report["checks"]:
        mark = "ok  " if check["ok"] else "warn"
        print(f"  [{mark}] {check['name']:<16} {check['detail']}")
        if not check["ok"] and check.get("hint"):
            print(f"         -> {check['hint']}")
    print()
    for name, ok in report["commands"].items():
        print(f"  [{'ok  ' if ok else 'warn'}] command {name}")
    if report["notes"]:
        print()
        for note in report["notes"]:
            print(f"  note: {note}")


def run_capture_test(config: Config) -> int:
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    screens = qt_screens()
    service = ScreenCaptureService(config)
    try:
        bundle = service.capture_all(screens=screens)
    except CaptureError as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        return 1
    paths = []
    for index, image in enumerate(bundle.images, start=1):
        path = Path(f"/tmp/heyclicky-capture-{index}.jpg")
        path.write_bytes(image.data)
        paths.append(str(path))
        print(
            f"screen {index}: {image.screen.width}x{image.screen.height} logical, "
            f"{image.pixel_width}x{image.pixel_height} captured, cursor={image.is_cursor_screen}"
        )
    print(f"backend: {bundle.backend}")
    print("saved: " + ", ".join(paths))
    return 0


# ---------------------------------------------------------------------------
# Qt application
# ---------------------------------------------------------------------------


def run_app(config: Config, args: argparse.Namespace) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("heyclicky")
    app.setApplicationDisplayName("Clicky")
    app.setDesktopFileName("heyclicky")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(theme.qss())

    overlay = OverlayManager(config=config)
    pipeline = Pipeline(config, overlay, welcome_on_start=not args.no_welcome)
    overlay.set_point_finished_callback(pipeline.on_point_finished)
    pipeline.audio_level.connect(overlay.set_audio_level)

    quitting = {"value": False}

    def quit_app() -> None:
        if quitting["value"]:
            return
        quitting["value"] = True
        pipeline.stop()
        try:
            tray.hide()
            ipc.close()
        except Exception:
            pass
        app.quit()

    panel = Panel(config, pipeline, on_quit=quit_app)
    # A screen-wide overlay cannot be raised above the always-on-top panel on
    # Wayland, so the panel draws pointing rings aimed at itself.
    pipeline.set_point_handler(
        lambda target: panel.show_point_marker(
            target.global_x, target.global_y, target.label or ""
        )
    )
    tray = Tray(pipeline)
    tray.panel_requested.connect(panel.show_near_cursor)
    tray.settings_requested.connect(lambda: (panel.show_near_cursor(), panel.open_settings()))
    tray.capture_requested.connect(panel.test_capture)
    tray.quit_requested.connect(quit_app)

    def handle_command(command: str) -> None:
        if command == "start":
            pipeline.start_listening()
        elif command == "stop":
            pipeline.stop_listening()
        elif command == "toggle":
            pipeline.toggle_listening()
        elif command == "panel":
            panel.show_near_cursor()
        elif command == "settings":
            panel.show_near_cursor()
            panel.open_settings()
        elif command == "capture":
            panel.show_near_cursor()
            panel.test_capture()
        elif command == "welcome":
            pipeline.show_welcome()
        elif command == "quit":
            quit_app()
        else:
            log.warning("unknown IPC command: %s", command)

    ipc = IpcServer(handle_command)
    if not ipc.listen():
        log.warning("could not create the single-instance socket; IPC commands disabled")

    app.aboutToQuit.connect(quit_app)
    pipeline.start()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning(
            "no system tray watcher detected; the panel still opens normally. "
            "On GNOME install/enable the AppIndicator extension for the top-bar icon."
        )
    # Show it regardless: with AppIndicator support Qt registers a
    # StatusNotifierItem that appears in the GNOME top bar.
    tray.show()

    show_panel = (
        args.show_panel
        or not bool(config.get("ui.onboarding_completed", False))
        or not QSystemTrayIcon.isSystemTrayAvailable()
        or not bool(config.get("ui.start_minimized", False))
    )
    if show_panel:
        QTimer.singleShot(350, panel.show_near_cursor)
    else:
        log.info("started minimized; click the tray icon or run --show-panel to open Clicky")

    # Keep Python references alive for the lifetime of the event loop.
    app._heyclicky = (overlay, pipeline, panel, tray, ipc)  # type: ignore[attr-defined]
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config_dir:
        os.environ["HEYCLICKY_CONFIG_DIR"] = str(Path(args.config_dir).expanduser())

    config = load_config()
    if args.provider:
        config.set("llm.provider", args.provider)
        if args.model:
            config.set(f"llm.{args.provider}.model", args.model)
        config.save()
    elif args.model:
        provider = str(config.get("llm.provider", "openrouter"))
        config.set(f"llm.{provider}.model", args.model)
        config.save()

    _configure_logging(config, args.log_level)

    if args.watch_keys is not None:
        try:
            return watch_keys(float(args.watch_keys))
        except ImportError:
            print("The evdev package is required: pip install evdev", file=sys.stderr)
            return 1

    if args.doctor:
        report = collect_doctor(config)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_doctor(report)
        return 0

    if args.capture_test:
        return run_capture_test(config)

    commands = [
        ("panel", args.show_panel),
        ("toggle", args.toggle_listen),
        ("start", args.start_listen),
        ("stop", args.stop_listen),
        ("settings", args.settings),
        ("quit", args.quit),
    ]
    requested = [name for name, enabled in commands if enabled]
    if requested:
        for command in requested:
            if not send_command(command):
                print("heyclicky is not running (no IPC socket found)", file=sys.stderr)
                return 1
        return 0

    # No command: attach to an existing instance, otherwise become the primary.
    if send_command("panel", timeout_ms=250):
        return 0

    try:
        return run_app(config, args)
    except Exception as exc:  # last-resort message instead of a bare traceback
        log.exception("fatal error")
        print(f"heyclicky failed to start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
