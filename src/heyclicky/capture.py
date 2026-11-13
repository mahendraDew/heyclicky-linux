"""Multi-monitor screen capture for Linux.

Clicky only looks at the screen when the user holds the push-to-talk shortcut.
This module captures every connected display, labels each image with its pixel
dimensions and cursor placement, and downscales to at most 1280px like the
original ScreenCaptureKit implementation.

Backends, in order of preference:

* ``x11``  - mss on an Xorg session; fastest and fully seamless.
* ``screencast`` - XDG ScreenCast portal + PipeWire; silent captures on Wayland
  after a one-time share prompt (recommended there).
* ``portal`` - xdg-desktop-portal screenshot; works on Wayland desktops whose
  portal supports non-interactive screenshots (GNOME 43+).
* ``gnome`` - GNOME Shell's private screenshot DBus API (X11 only; GNOME 42+
  gates it behind the shell's unsafe mode).
* ``cli`` - gnome-screenshot / grim / spectacle / scrot, whichever exists.
* ``mock`` - deterministic images for tests.

On a Wayland session no client can silently read the whole screen by design.
ScreenCast is the supported route: the user shares the monitor(s) once and the
app captures frames from the PipeWire stream afterwards.
"""

from __future__ import annotations

import atexit
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from .config import Config
from .geometry import ScreenGeometry, screen_for_point
from .prompts import screen_label


class CaptureError(RuntimeError):
    """A user-facing screen capture failure with an actionable message."""


@dataclass(frozen=True)
class ScreenInfo:
    name: str
    x: int
    y: int
    width: int
    height: int
    device_pixel_ratio: float = 1.0

    @property
    def geometry(self) -> ScreenGeometry:
        return ScreenGeometry(
            name=self.name,
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            device_pixel_ratio=self.device_pixel_ratio,
        )

    def contains(self, global_x: float, global_y: float) -> bool:
        return self.geometry.contains(global_x, global_y)


@dataclass
class CaptureImage:
    """One display's JPEG plus everything needed to map model coordinates."""

    screen: ScreenInfo
    data: bytes
    pixel_width: int
    pixel_height: int
    label: str
    is_cursor_screen: bool
    mime_type: str = "image/jpeg"

    @property
    def data_url(self) -> str:
        import base64

        return f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"


@dataclass
class CaptureBundle:
    images: list[CaptureImage]
    backend: str
    captured_at: float

    @property
    def cursor_image(self) -> CaptureImage | None:
        for image in self.images:
            if image.is_cursor_screen:
                return image
        return self.images[0] if self.images else None


# ---------------------------------------------------------------------------
# Qt screen enumeration (lazy import keeps capture.py testable without a GUI)
# ---------------------------------------------------------------------------


def qt_screens() -> list[ScreenInfo]:
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        raise CaptureError("Qt application is not running")
    screens: list[ScreenInfo] = []
    for index, screen in enumerate(app.screens()):
        geometry = screen.geometry()
        # Qt names can be empty on some backends; keep a stable label.
        name = screen.name() or f"screen-{index + 1}"
        screens.append(
            ScreenInfo(
                name=name,
                x=geometry.x(),
                y=geometry.y(),
                width=geometry.width(),
                height=geometry.height(),
                device_pixel_ratio=max(1.0, float(screen.devicePixelRatio())),
            )
        )
    return screens


def qt_cursor_position() -> tuple[float, float]:
    from PyQt6.QtGui import QCursor

    point = QCursor.pos()
    return float(point.x()), float(point.y())


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class CaptureBackend(ABC):
    id: str = "base"
    display_name: str = "Base"

    def __init__(self, config: Config | None = None) -> None:
        self.config = config

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    def capture_screens(self, screens: list[ScreenInfo]) -> list[tuple[ScreenInfo, Image.Image]]:
        """Return one PIL image per requested screen."""

    @staticmethod
    def _capture_desktop_with_cli(command: list[str]) -> Image.Image:
        handle, path = tempfile.mkstemp(prefix="heyclicky-shot-", suffix=".png")
        os.close(handle)
        try:
            result = subprocess.run(command + [path], capture_output=True, text=True, timeout=45)
            if result.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
                raise CaptureError(
                    f"{command[0]} failed: {(result.stderr or result.stdout).strip()[:300]}"
                )
            with Image.open(path) as image:
                return image.convert("RGB").copy()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _crop_screens_from_desktop(
        desktop: Image.Image, screens: list[ScreenInfo]
    ) -> list[tuple[ScreenInfo, Image.Image]]:
        """Crop a full-desktop capture into per-screen images.

        The screenshot is assumed to cover the union of the logical screen
        rectangles. This is exact for single-monitor and uniform-scale setups
        and remains correct on mixed-DPI desktops as long as the compositor's
        screenshot uses the same union scaling.
        """
        if not screens:
            return []
        min_x = min(screen.x for screen in screens)
        min_y = min(screen.y for screen in screens)
        max_x = max(screen.x + screen.width for screen in screens)
        max_y = max(screen.y + screen.height for screen in screens)
        virtual_width = max(1, max_x - min_x)
        virtual_height = max(1, max_y - min_y)
        scale_x = desktop.width / virtual_width
        scale_y = desktop.height / virtual_height

        results: list[tuple[ScreenInfo, Image.Image]] = []
        for screen in screens:
            left = round((screen.x - min_x) * scale_x)
            top = round((screen.y - min_y) * scale_y)
            right = round((screen.x - min_x + screen.width) * scale_x)
            bottom = round((screen.y - min_y + screen.height) * scale_y)
            left = max(0, min(left, desktop.width - 1))
            top = max(0, min(top, desktop.height - 1))
            right = max(left + 1, min(right, desktop.width))
            bottom = max(top + 1, min(bottom, desktop.height))
            results.append((screen, desktop.crop((left, top, right, bottom))))
        return results


class X11MSSBackend(CaptureBackend):
    id = "x11"
    display_name = "X11 (mss)"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("DISPLAY"):
            return False, "DISPLAY is not set (not an X11 session)"
        try:
            import mss  # noqa: F401
        except ImportError:
            return False, "python package 'mss' is not installed"
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            return True, "XWayland only; native Wayland windows will be missing"
        return True, "X11 capture ready"

    def capture_screens(self, screens: list[ScreenInfo]) -> list[tuple[ScreenInfo, Image.Image]]:
        import mss

        results: list[tuple[ScreenInfo, Image.Image]] = []
        with mss.mss() as sct:
            monitors = sct.monitors[1:]  # index 0 is the virtual union
            for index, screen in enumerate(screens):
                region = None
                # Prefer a monitor whose physical size matches this screen.
                expected_w = round(screen.width * screen.device_pixel_ratio)
                expected_h = round(screen.height * screen.device_pixel_ratio)
                for monitor in monitors:
                    if (
                        abs(monitor["width"] - expected_w) <= 2
                        and abs(monitor["height"] - expected_h) <= 2
                        and abs(monitor["left"] - round(screen.x * screen.device_pixel_ratio)) <= 2
                    ):
                        region = monitor
                        break
                if region is None and index < len(monitors):
                    region = monitors[index]
                if region is None:
                    region = {
                        "left": round(screen.x * screen.device_pixel_ratio),
                        "top": round(screen.y * screen.device_pixel_ratio),
                        "width": expected_w,
                        "height": expected_h,
                    }
                try:
                    shot = sct.grab(region)
                    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                except Exception as exc:
                    raise CaptureError(f"X11 capture failed: {exc}") from exc
                results.append((screen, image))
        return results


class GnomeShellBackend(CaptureBackend):
    id = "gnome"
    display_name = "GNOME Shell DBus"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("gdbus"):
            return False, "gdbus is not installed"
        if "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            return False, "not a GNOME session"
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            # GNOME 42+ only allows its own shell to call this interface on
            # Wayland; other clients get AccessDenied. The XDG portal is the
            # supported path there.
            return False, "GNOME blocks its screenshot API on Wayland (use the portal)"
        return True, "GNOME Shell screenshot"

    def capture_screens(self, screens: list[ScreenInfo]) -> list[tuple[ScreenInfo, Image.Image]]:
        handle, path = tempfile.mkstemp(prefix="heyclicky-gnome-", suffix=".png")
        os.close(handle)
        try:
            result = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.gnome.Shell.Screenshot",
                    "--object-path",
                    "/org/gnome/Shell/Screenshot",
                    "--method",
                    "org.gnome.Shell.Screenshot.Screenshot",
                    "false",
                    "false",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
                detail = (result.stderr or result.stdout).strip()[:300]
                raise CaptureError(f"GNOME Shell refused the screenshot: {detail}")
            with Image.open(path) as image:
                desktop = image.convert("RGB").copy()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return self._crop_screens_from_desktop(desktop, screens)


class PortalScreenshotBackend(CaptureBackend):
    id = "portal"
    display_name = "XDG desktop portal"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("gdbus"):
            return False, "gdbus is not installed"
        if _import_gi() is None:
            return False, "python3-gi is not installed (sudo apt install python3-gi)"
        return True, "portal screenshot (a desktop prompt may appear)"

    def capture_screens(self, screens: list[ScreenInfo]) -> list[tuple[ScreenInfo, Image.Image]]:
        path = _portal_screenshot()
        try:
            with Image.open(path) as image:
                desktop = image.convert("RGB").copy()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return self._crop_screens_from_desktop(desktop, screens)


class CliScreenshotBackend(CaptureBackend):
    id = "cli"
    display_name = "Screenshot CLI"

    CANDIDATES: list[tuple[str, list[str]]] = [
        ("gnome-screenshot", ["gnome-screenshot", "-f"]),
        ("grim", ["grim"]),
        ("spectacle", ["spectacle", "-b", "-n", "-o"]),
        ("scrot", ["scrot", "-o"]),
    ]

    def _command(self) -> tuple[str, list[str]] | None:
        for binary, command in self.CANDIDATES:
            if shutil.which(binary):
                return binary, command
        return None

    def available(self) -> tuple[bool, str]:
        command = self._command()
        if command is None:
            return False, "no screenshot CLI found (install gnome-screenshot, grim or scrot)"
        return True, f"{command[0]} ready"

    def capture_screens(self, screens: list[ScreenInfo]) -> list[tuple[ScreenInfo, Image.Image]]:
        command = self._command()
        if command is None:
            raise CaptureError("No screenshot tool is installed.")
        desktop = self._capture_desktop_with_cli(command[1])
        return self._crop_screens_from_desktop(desktop, screens)


class MockBackend(CaptureBackend):
    """Deterministic backend used by tests and ``--doctor``."""

    id = "mock"
    display_name = "Mock"

    def available(self) -> tuple[bool, str]:
        return True, "mock capture"

    def capture_screens(self, screens: list[ScreenInfo]) -> list[tuple[ScreenInfo, Image.Image]]:
        results = []
        for index, screen in enumerate(screens):
            image = Image.new("RGB", (min(screen.width, 640), min(screen.height, 400)), (24, 26, 30))
            # A few blocks make the image non-uniform, which is enough to test
            # encoding, labeling and coordinate mapping.
            for x in range(0, image.width, 40):
                for y in range(0, image.height, 40):
                    image.putpixel((x, y), (50 + index * 20, 90, 200))
            results.append((screen, image))
        return results


BACKENDS: dict[str, type[CaptureBackend]] = {
    "x11": X11MSSBackend,
    "gnome": GnomeShellBackend,
    "portal": PortalScreenshotBackend,
    "cli": CliScreenshotBackend,
    "mock": MockBackend,
}


# PyGObject ships with the distribution's Python, not with the virtualenv the
# app is installed into. Append the system dist-packages directory (at the end,
# so venv packages still win) before giving up on the portal.
_SYSTEM_DIST_PACKAGES = (
    "/usr/lib/python3/dist-packages",
    "/usr/local/lib/python3/dist-packages",
)


def _import_gi():
    """Import PyGObject, borrowing the system copy inside a virtualenv."""
    try:
        import gi  # noqa: F401
    except ImportError:
        for path in _SYSTEM_DIST_PACKAGES:
            if os.path.isdir(path) and path not in sys.path:
                sys.path.append(path)
        try:
            import gi  # noqa: F811
        except ImportError:
            return None
    return gi


_PORTAL_BUS = None
_PORTAL_BUS_LOCK = threading.Lock()


def _portal_bus():
    """Return a process-wide session-bus connection.

    Portal session handles embed the caller's unique bus name, so every call
    for one session must use the *same* connection. Holding a reference also
    stops PyGObject from dropping the connection between calls.
    """
    global _PORTAL_BUS
    with _PORTAL_BUS_LOCK:
        if _PORTAL_BUS is None:
            _import_gi()
            from gi.repository import Gio

            _PORTAL_BUS = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return _PORTAL_BUS


def _portal_timeout(loop, outcome: dict) -> bool:
    outcome["timed_out"] = True
    loop.quit()
    return False


def _portal_call(
    interface: str,
    method: str,
    signature: str,
    values: tuple,
    *,
    timeout_ms: int = 60_000,
) -> tuple[int, dict]:
    """Call a portal request method and wait for its ``Response`` signal.

    The last element of ``values`` must be the method's ``a{sv}`` options dict.
    A ``handle_token`` is added so the request path is known up front. The
    signal is subscribed *before* the call because the portal can finish the
    request before ``call_sync`` returns, and a late subscription would wait
    forever (signals are not replayed).
    """
    _import_gi()
    from gi.repository import Gio, GLib

    bus = _portal_bus()
    token = f"heyclicky_{uuid.uuid4().hex[:12]}"
    args = list(values)
    options = dict(args[-1]) if args else {}
    options["handle_token"] = GLib.Variant("s", token)
    args[-1] = options

    unique_name = (bus.get_unique_name() or "").lstrip(":")
    expected_path = (
        f"/org/freedesktop/portal/desktop/request/"
        f"{unique_name.replace('.', '_')}/{token}"
    )

    loop_context = GLib.MainContext.new()
    loop = GLib.MainLoop(loop_context)
    outcome: dict = {"done": False, "response": -1, "results": {}}
    accepted_paths: set[str] = set()

    def on_response(_connection, _sender, object_path, _interface, _signal, parameters, *_):
        if object_path not in accepted_paths:
            return
        response, results = parameters.unpack()
        outcome["response"] = response
        outcome["results"] = results or {}
        outcome["done"] = True
        loop.quit()

    def subscribe(path: str) -> int:
        accepted_paths.add(path)
        return bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        )

    # Use a private main context: Qt installs its own GLib context on the GUI
    # thread, and relying on the default context makes responses get lost when
    # capture runs on a worker thread.
    loop_context.push_thread_default()
    try:
        subscription = subscribe(expected_path)
        timeout_id = GLib.timeout_add_seconds(
            120, lambda: _portal_timeout(loop, outcome)
        )
    finally:
        loop_context.pop_thread_default()

    try:
        result = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            interface,
            method,
            GLib.Variant(signature, tuple(args)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
    except Exception as exc:
        bus.signal_unsubscribe(subscription)
        GLib.source_remove(timeout_id)
        raise CaptureError(f"{interface}.{method} failed: {exc}") from exc

    request_path = result.unpack()[0]
    if request_path != expected_path:
        # The portal canonicalised the path; resubscribe to the real one.
        bus.signal_unsubscribe(subscription)
        loop_context.push_thread_default()
        try:
            subscription = subscribe(request_path)
        finally:
            loop_context.pop_thread_default()

    try:
        if not outcome["done"]:
            loop.run()
    finally:
        if not outcome.get("timed_out"):
            GLib.source_remove(timeout_id)
        bus.signal_unsubscribe(subscription)

    if outcome.get("timed_out"):
        raise CaptureError(
            f"{interface}.{method} timed out waiting for the desktop prompt "
            "(click Share / Allow in the dialog)"
        )
    if not outcome["done"]:
        raise CaptureError(f"{interface}.{method} timed out (no response)")
    return int(outcome["response"]), dict(outcome["results"])


def _portal_screenshot() -> str:
    """Take one non-interactive screenshot through the XDG portal."""
    _import_gi()
    from gi.repository import GLib

    response, results = _portal_call(
        "org.freedesktop.portal.Screenshot",
        "Screenshot",
        "(sa{sv})",
        (
            "",
            {
                "interactive": GLib.Variant("b", False),
                "modal": GLib.Variant("b", False),
            },
        ),
    )
    if response != 0 or not results.get("uri"):
        raise CaptureError(
            "portal screenshot was cancelled or denied "
            "(GNOME 42 only supports interactive screenshots; use ScreenCast)"
        )
    uri = str(results["uri"])
    if not uri.startswith("file://"):
        raise CaptureError(f"portal returned an unsupported URI: {uri}")
    from urllib.parse import unquote, urlparse

    return unquote(urlparse(uri).path)


# ---------------------------------------------------------------------------
# ScreenCast portal + PipeWire (silent captures on Wayland)
# ---------------------------------------------------------------------------

_gst_lock = threading.Lock()
_gst_ready = False


def _import_gst():
    """Import GStreamer, initialising it once."""
    global _gst_ready
    if _import_gi() is None:
        return None
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return None
    with _gst_lock:
        if not _gst_ready:
            Gst.init(None)
            _gst_ready = True
    return Gst


class _StreamGrabber:
    """Keeps the newest frame of one PipeWire screencast stream."""

    def __init__(self, node_id: int) -> None:
        Gst = _import_gst()
        if Gst is None:
            raise CaptureError(
                "GStreamer with PipeWire support is required for screen sharing "
                "(sudo apt install python3-gi gstreamer1.0-pipewire)"
            )
        self._Gst = Gst
        self._latest: tuple[int, int, bytes] | None = None
        self._lock = threading.Lock()
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc path={node_id} do-timestamp=true "
            "! videoconvert "
            "! video/x-raw,format=RGB "
            "! appsink name=heyclicky_sink max-buffers=1 drop=true "
            "sync=false emit-signals=true"
        )
        sink = self._pipeline.get_by_name("heyclicky_sink")
        if sink is None:
            raise CaptureError("could not build the PipeWire capture pipeline")
        self._sink = sink
        self._sink.connect("new-sample", self._on_sample)
        # Start lazily: a PLAYING screencast pipeline keeps converting full
        # frames at the monitor refresh rate, which burns a whole core while
        # Clicky is idle. Grabs start it for a moment and stop it again.
        self._playing = False
        self._pipeline.set_state(Gst.State.NULL)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self._Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        structure = sample.get_caps().get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        ok, info = buffer.map(self._Gst.MapFlags.READ)
        if not ok:
            return self._Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buffer.unmap(info)
        with self._lock:
            self._latest = (width, height, data)
        return self._Gst.FlowReturn.OK

    def grab(self, timeout: float = 4.0) -> Image.Image:
        self._start()
        try:
            deadline = time.monotonic() + timeout
            while True:
                with self._lock:
                    latest = self._latest
                if latest is not None:
                    width, height, data = latest
                    return Image.frombuffer(
                        "RGB", (width, height), data, "raw", "RGB", 0, 1
                    ).copy()
                if time.monotonic() >= deadline:
                    raise CaptureError(
                        "the screen share produced no frames. Stop the share and "
                        "try again, selecting a monitor."
                    )
                time.sleep(0.05)
        finally:
            self.stop()

    def _start(self) -> None:
        if self._playing:
            return
        with self._lock:
            self._latest = None
        self._pipeline.set_state(self._Gst.State.PLAYING)
        self._playing = True

    def stop(self) -> None:
        if not self._playing:
            return
        self._playing = False
        try:
            self._pipeline.set_state(self._Gst.State.NULL)
        except Exception:
            pass


def _portal_close_session(handle: str) -> None:
    _import_gi()
    from gi.repository import Gio

    bus = _portal_bus()
    bus.call_sync(
        "org.freedesktop.portal.Desktop",
        handle,
        "org.freedesktop.portal.Session",
        "Close",
        None,
        None,
        Gio.DBusCallFlags.NONE,
        5_000,
        None,
    )


class _ScreenCastSession:
    """One long-lived ScreenCast session shared by all captures."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.session_handle: str | None = None
        self.streams: list[tuple[int, dict]] = []
        self._grabbers: dict[int, _StreamGrabber] = {}

    def ensure(self, config: Config | None) -> None:
        with self._lock:
            if self.session_handle is not None:
                return
            _import_gi()
            from gi.repository import GLib

            session_token = f"heyclicky_{uuid.uuid4().hex[:12]}"
            response, results = _portal_call(
                "org.freedesktop.portal.ScreenCast",
                "CreateSession",
                "(a{sv})",
                ({"session_handle_token": GLib.Variant("s", session_token)},),
            )
            if response != 0:
                raise CaptureError(
                    "the desktop refused to create a screen-share session"
                )
            handle = str(results.get("session_handle") or "")
            if not handle:
                raise CaptureError("the portal returned no screen-share session")

            restore_token = ""
            if config is not None:
                restore_token = str(
                    config.get("capture.screencast_restore_token", "") or ""
                )
            select_options = {
                "types": GLib.Variant("u", 1),  # monitors
                "multiple": GLib.Variant("b", True),
                "cursor_mode": GLib.Variant("u", 1),  # hidden; Qt draws the pointer
                "persist_mode": GLib.Variant("u", 2),  # remember across restarts
            }
            if restore_token:
                select_options["restore_token"] = GLib.Variant("s", restore_token)

            try:
                response, _ = _portal_call(
                    "org.freedesktop.portal.ScreenCast",
                    "SelectSources",
                    "(oa{sv})",
                    (handle, select_options),
                )
                if response != 0:
                    raise CaptureError(
                        "screen sharing was cancelled. Press the shortcut again and "
                        "select the monitor(s) to share."
                    )

                response, results = _portal_call(
                    "org.freedesktop.portal.ScreenCast",
                    "Start",
                    "(osa{sv})",
                    (handle, "", {}),
                )
                if response != 0:
                    raise CaptureError("screen sharing was cancelled")

                streams = results.get("streams") or []
                if not streams:
                    raise CaptureError(
                        "the portal started a session without streams"
                    )
            except Exception:
                # Do not leak a half-started portal session.
                try:
                    _portal_close_session(handle)
                except Exception:
                    pass
                raise

            self.session_handle = handle
            self.streams = [
                (int(node), dict(props or {})) for node, props in streams
            ]
            for node, _props in self.streams:
                self._grabbers[node] = _StreamGrabber(node)

            token = results.get("restore_token")
            if token and config is not None:
                config.set("capture.screencast_restore_token", str(token))
                try:
                    config.save()
                except OSError:
                    pass

    def grab(self, node: int, timeout: float = 4.0) -> Image.Image:
        grabber = self._grabbers.get(node)
        if grabber is None:
            raise CaptureError("the screen-share stream is no longer available")
        return grabber.grab(timeout)

    def close(self) -> None:
        with self._lock:
            for grabber in self._grabbers.values():
                grabber.stop()
            self._grabbers.clear()
            self.streams = []
            handle = self.session_handle
            self.session_handle = None
        if handle:
            try:
                _portal_close_session(handle)
            except Exception:
                pass


_SCREENCAST_SESSION: _ScreenCastSession | None = None
_SCREENCAST_SESSION_LOCK = threading.Lock()


def _screen_cast_session() -> _ScreenCastSession:
    global _SCREENCAST_SESSION
    with _SCREENCAST_SESSION_LOCK:
        if _SCREENCAST_SESSION is None:
            _SCREENCAST_SESSION = _ScreenCastSession()
        return _SCREENCAST_SESSION


def _close_screencast_session() -> None:
    session = _SCREENCAST_SESSION
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def _unpack_pair(value) -> tuple | None:
    if hasattr(value, "unpack"):
        value = value.unpack()
    try:
        pair = tuple(value)
    except TypeError:
        return None
    return pair if len(pair) == 2 else None


def _match_streams(
    streams: list[tuple[int, dict]], screens: list[ScreenInfo]
) -> list[tuple[ScreenInfo, int]]:
    """Match portal streams to Qt screens by their compositor position."""
    pairs: list[tuple[ScreenInfo, int]] = []
    used: set[int] = set()
    for screen in screens:
        for index, (_node, props) in enumerate(streams):
            if index in used:
                continue
            position = _unpack_pair(props.get("position"))
            if position is None:
                continue
            if abs(position[0] - screen.x) <= 2 and abs(position[1] - screen.y) <= 2:
                used.add(index)
                pairs.append((screen, int(streams[index][0])))
                break
    if not pairs:
        # Desktops that do not report positions: fall back to stream order.
        for index, screen in enumerate(screens):
            if index < len(streams):
                pairs.append((screen, int(streams[index][0])))
    return pairs


class PortalScreenCastBackend(CaptureBackend):
    id = "screencast"
    display_name = "XDG ScreenCast (PipeWire)"

    def available(self) -> tuple[bool, str]:
        Gst = _import_gst()
        if Gst is None:
            return False, (
                "GStreamer/PyGObject missing "
                "(sudo apt install python3-gi gstreamer1.0-pipewire)"
            )
        for name in ("pipewiresrc", "appsink"):
            if Gst.ElementFactory.find(name) is None:
                return False, (
                    f"GStreamer element {name!r} is missing "
                    "(sudo apt install gstreamer1.0-pipewire)"
                )
        return True, "ScreenCast portal (share prompt on first use)"

    def capture_screens(
        self, screens: list[ScreenInfo]
    ) -> list[tuple[ScreenInfo, Image.Image]]:
        session = _screen_cast_session()
        try:
            session.ensure(self.config)
            pairs = _match_streams(session.streams, screens)
            if not pairs:
                raise CaptureError(
                    "none of the shared monitors matched this desktop's screens. "
                    "Press the shortcut again and share the monitor(s) Clicky should see."
                )
            return [(screen, session.grab(node)) for screen, node in pairs]
        except CaptureError:
            # A stream can die (share stopped, monitor unplugged). Drop the
            # session so the next capture starts a fresh one instead of
            # failing forever.
            session.close()
            raise


atexit.register(_close_screencast_session)

BACKENDS["screencast"] = PortalScreenCastBackend


# ---------------------------------------------------------------------------
# High level capture service
# ---------------------------------------------------------------------------


class ScreenCaptureService:
    def __init__(
        self,
        config: Config,
        *,
        screens_provider: Callable[[], list[ScreenInfo]] | None = None,
        cursor_provider: Callable[[], tuple[float, float]] | None = None,
        overlay_hider: Callable[[], None] | None = None,
        overlay_shower: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.screens_provider = screens_provider or qt_screens
        self.cursor_provider = cursor_provider or qt_cursor_position
        self.overlay_hider = overlay_hider
        self.overlay_shower = overlay_shower
        self.last_error: str | None = None
        self.last_backend: str | None = None
        self._backend: CaptureBackend | None = None

    def _resolve_backend(self) -> CaptureBackend:
        requested = str(self.config.get("capture.backend", "auto")).lower()
        if requested != "auto":
            backend_class = BACKENDS.get(requested)
            if backend_class is None:
                raise CaptureError(f"unknown capture backend: {requested}")
            backend = backend_class(self.config)
            ok, detail = backend.available()
            if not ok:
                raise CaptureError(f"{backend.display_name} unavailable: {detail}")
            return backend

        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "wayland":
            order = ["screencast", "portal", "gnome", "cli", "x11"]
        else:
            order = ["x11", "portal", "screencast", "gnome", "cli"]
        reasons: list[str] = []
        for backend_id in order:
            backend = BACKENDS[backend_id](self.config)
            ok, detail = backend.available()
            if ok:
                return backend
            reasons.append(f"{backend.display_name}: {detail}")
        raise CaptureError("No screen capture backend is available. " + " | ".join(reasons))

    def backend_name(self) -> str:
        try:
            return self._resolve_backend().display_name
        except CaptureError as exc:
            return f"unavailable ({exc})"

    def capture_all(
        self,
        screens: list[ScreenInfo] | None = None,
        cursor: tuple[float, float] | None = None,
    ) -> CaptureBundle:
        """Capture every display.

        ``screens`` and ``cursor`` let a worker thread reuse a snapshot taken
        on the Qt GUI thread (QScreen/QCursor are not safe to read off-thread).
        """
        screens = list(screens) if screens is not None else self.screens_provider()
        if not screens:
            raise CaptureError("No displays were reported by the desktop.")

        backend = self._resolve_backend()
        self._backend = backend
        hid_overlay = False
        if self.overlay_hider is not None and self.config.get("capture.hide_overlay", True):
            try:
                self.overlay_hider()
                hid_overlay = True
                # Give the compositor a frame to remove our own overlay.
                time.sleep(0.12)
            except Exception:
                hid_overlay = False
        try:
            raw = backend.capture_screens(screens)
        except CaptureError as exc:
            self.last_error = str(exc)
            raise
        except Exception as exc:
            self.last_error = f"{backend.display_name} capture failed: {exc}"
            raise CaptureError(self.last_error) from exc
        finally:
            if hid_overlay and self.overlay_shower is not None:
                try:
                    self.overlay_shower()
                except Exception:
                    pass

        if not raw:
            raise CaptureError("The capture backend returned no images.")

        cursor_x, cursor_y = cursor if cursor is not None else self.cursor_provider()
        cursor_screen = screen_for_point([screen.geometry for screen in screens], cursor_x, cursor_y)
        # Sort the cursor screen first so "screen 1" is always primary focus.
        raw.sort(key=lambda pair: 0 if cursor_screen and pair[0].name == cursor_screen.name else 1)

        max_dimension = int(self.config.get("capture.max_dimension", 1280))
        quality = int(self.config.get("capture.jpeg_quality", 80))
        encoded: list[CaptureImage] = []
        total = len(raw)
        for index, (screen, image) in enumerate(raw):
            is_cursor_screen = bool(cursor_screen and screen.name == cursor_screen.name)
            image = self._resize(image, max_dimension)
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
            pixel_width, pixel_height = image.size
            encoded.append(
                CaptureImage(
                    screen=screen,
                    data=buffer.getvalue(),
                    pixel_width=pixel_width,
                    pixel_height=pixel_height,
                    label=screen_label(index, total, is_cursor_screen, pixel_width, pixel_height),
                    is_cursor_screen=is_cursor_screen,
                )
            )

        self.last_backend = backend.display_name
        self.last_error = None
        return CaptureBundle(images=encoded, backend=backend.display_name, captured_at=time.time())

    @staticmethod
    def _resize(image: Image.Image, max_dimension: int) -> Image.Image:
        width, height = image.size
        largest = max(width, height)
        if largest <= max_dimension:
            return image
        scale = max_dimension / float(largest)
        return image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
