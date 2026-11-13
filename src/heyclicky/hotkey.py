"""System-wide push-to-talk hotkeys on Linux.

Three real backends are supported:

* ``evdev`` - reads /dev/input directly. The only backend that gives true
  press/release push-to-talk on both X11 and Wayland, and the only one that can
  see ``KEY_FN`` when a keyboard's firmware reports it. Requires the user to be
  in the ``input`` group (one-time ``sudo usermod -aG input $USER``).
* ``pynput`` - X11 only. Works out of the box on an Xorg session.
* ``gnome`` - installs a GNOME custom keybinding that calls
  ``heyclicky --toggle-listen``. Toggle rather than hold-to-talk, but it needs
  no elevated permissions and works on Wayland.

The Fn key is handled by the embedded controller on most laptops and never
reaches the kernel; :func:`fn_key_supported` detects this and the resolver falls
back to another chord instead of silently binding just Super.

An in-app button and tray menu item always work, so the app is usable even when
the desktop refuses global shortcuts.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

MODIFIER_CODES = {
    "ctrl": {"KEY_LEFTCTRL", "KEY_RIGHTCTRL"},
    "alt": {"KEY_LEFTALT", "KEY_RIGHTALT"},
    "shift": {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"},
    "super": {"KEY_LEFTMETA", "KEY_RIGHTMETA"},
    # Only present on keyboards whose firmware reports the Fn key itself.
    # Most laptop Fn keys are handled by the embedded controller and never
    # reach the kernel, so this is best-effort by design.
    "fn": {"KEY_FN"},
}

PYNPUT_KEYS = {
    "ctrl": {"ctrl", "ctrl_l", "ctrl_r"},
    "alt": {"alt", "alt_l", "alt_r", "alt_gr"},
    "shift": {"shift", "shift_l", "shift_r"},
    "super": {"cmd", "cmd_l", "cmd_r"},
}

FN_KEY_CODE = 464  # KEY_FN in linux/input-event-codes.h


class HotkeyError(RuntimeError):
    pass


def parse_combo(combo: str) -> list[str]:
    parts = [part.strip().lower() for part in (combo or "").split("+") if part.strip()]
    if not parts:
        parts = ["ctrl", "alt"]
    aliases = {
        "control": "ctrl",
        "option": "alt",
        "meta": "super",
        "cmd": "super",
        "win": "super",
        "windows": "super",
        "function": "fn",
    }
    normalised = [aliases.get(part, part) for part in parts]
    for part in normalised:
        if part not in MODIFIER_CODES:
            raise HotkeyError(f"unsupported hotkey modifier: {part}")
    return normalised


def hotkey_label(combo: str) -> str:
    labels = {
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "super": "Super",
        "fn": "Fn",
    }
    try:
        return "+".join(labels[part] for part in parse_combo(combo))
    except HotkeyError:
        return combo


def _key_codes_from_capability(path: str) -> set[int]:
    """Decode a sysfs ``capabilities/key`` bitmap into key codes.

    sysfs prints the bitmap most-significant word first, so the last word holds
    bits 0-63. Reading it in the other order silently produces nonsense such as
    'no Ctrl key on a laptop keyboard'.
    """
    try:
        with open(path, encoding="ascii") as handle:
            words = handle.read().split()
    except OSError:
        return set()
    bitmap = 0
    for index, chunk in enumerate(reversed(words)):
        try:
            bitmap |= int(chunk, 16) << (64 * index)
        except ValueError:
            return set()
    return {code for code in range(768) if (bitmap >> code) & 1}


def fn_key_supported(sysfs_root: str = "/sys/class/input") -> tuple[bool, str]:
    """Report whether any keyboard advertises KEY_FN.

    On most laptops (including Lenovo IdeaPads) the Fn key is consumed by the
    embedded controller and never generates an input event, so no software
    shortcut can observe it. When a keyboard *does* advertise KEY_FN, the evdev
    backend can use it like any other modifier.
    """
    keyboards = 0
    for path in sorted(glob.glob(os.path.join(sysfs_root, "event*/device/capabilities/key"))):
        codes = _key_codes_from_capability(path)
        # A keyboard advertises Ctrl and letter keys. Ignore power buttons,
        # touchpads, audio jacks and other event nodes.
        if 29 not in codes or 97 not in codes:
            continue
        keyboards += 1
        if FN_KEY_CODE in codes:
            event = path.split("/")[-4]
            return True, f"reported by {event} as KEY_FN"
    if keyboards == 0:
        return False, "no keyboard capability data found"
    return False, (
        "this keyboard's firmware does not report the Fn key (it never reaches "
        "the kernel, so no shortcut can use it)"
    )


class HotkeyBackend(ABC):
    id: str = "base"
    display_name: str = "Base"
    toggle_mode = False

    def __init__(self, combo: str = "ctrl+alt") -> None:
        self.combo = parse_combo(combo)
        self._on_press: Callable[[], None] = lambda: None
        self._on_release: Callable[[], None] = lambda: None
        self.note = ""
        self.requested_combo: str | None = None

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        ...

    @abstractmethod
    def start(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    def detail(self) -> str:
        mode = "toggle" if self.toggle_mode else "hold"
        text = f"{self.display_name} ({hotkey_label('+'.join(self.combo))}, {mode})"
        if self.note:
            text = f"{text} — {self.note}"
        return text


class EvdevHotkeyBackend(HotkeyBackend):
    id = "evdev"
    display_name = "evdev global hotkey"

    def __init__(self, combo: str = "ctrl+alt") -> None:
        super().__init__(combo)
        self._devices: list = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pressed_modifiers: set[str] = set()
        self._is_down = False
        self._lock = threading.Lock()
        self._fn_exposed = False

    def _find_devices(self) -> list:
        try:
            from evdev import InputDevice, list_devices
        except ImportError:
            return []
        found = []
        self._fn_exposed = False
        for path in list_devices():
            try:
                device = InputDevice(path)
            except (PermissionError, OSError):
                continue
            capabilities = device.capabilities()
            key_codes = set(capabilities.get(1, []))  # EV_KEY == 1
            # A keyboard has at least a few letter keys and both ctrl keys.
            if 29 in key_codes and 97 in key_codes:
                found.append(device)
                if FN_KEY_CODE in key_codes:
                    self._fn_exposed = True
            else:
                device.close()
        return found

    def available(self) -> tuple[bool, str]:
        try:
            import evdev  # noqa: F401
        except ImportError:
            return False, "python package 'evdev' is not installed"
        devices = self._find_devices()
        if not devices:
            return False, (
                "no readable keyboards (add your user to the input group: "
                "sudo usermod -aG input $USER, then log out and back in)"
            )
        for device in devices:
            device.close()
        if "fn" in self.combo and not self._fn_exposed:
            return False, "the Fn key is not reported by any readable keyboard"
        return True, f"{len(devices)} keyboard device(s) readable"

    def start(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._devices = self._find_devices()
        if not self._devices:
            raise HotkeyError("evdev could not open any keyboard")
        if "fn" in self.combo and not self._fn_exposed:
            for device in self._devices:
                device.close()
            self._devices = []
            raise HotkeyError("this keyboard does not report the Fn key")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="heyclicky-hotkey", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import select

        from evdev import ecodes

        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select(self._devices, [], [], 0.3)
            except (OSError, ValueError):
                break
            for device in ready:
                try:
                    for event in device.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        name = ecodes.KEY.get(event.code)
                        if not isinstance(name, str):
                            continue
                        self._handle_key(name, event.value)
                except OSError:
                    continue

    def _handle_key(self, key_name: str, value: int) -> None:
        with self._lock:
            for modifier, codes in MODIFIER_CODES.items():
                if key_name in codes:
                    if value == 1:  # key down
                        self._pressed_modifiers.add(modifier)
                    elif value == 0:  # key up
                        self._pressed_modifiers.discard(modifier)
                    break
            satisfied = all(modifier in self._pressed_modifiers for modifier in self.combo)
            if satisfied and not self._is_down:
                self._is_down = True
                threading.Thread(target=self._on_press, daemon=True).start()
            elif not satisfied and self._is_down:
                self._is_down = False
                threading.Thread(target=self._on_release, daemon=True).start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        for device in self._devices:
            try:
                device.close()
            except OSError:
                pass
        self._devices = []


class PynputHotkeyBackend(HotkeyBackend):
    id = "pynput"
    display_name = "X11 global hotkey"

    def __init__(self, combo: str = "ctrl+alt") -> None:
        super().__init__(combo)
        self._listener = None
        self._pressed: set[str] = set()
        self._is_down = False
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        if "fn" in self.combo:
            return False, "the Fn key is not visible to X11 applications"
        if not os.environ.get("DISPLAY"):
            return False, "DISPLAY is not set"
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            return False, "pynput cannot see native Wayland windows"
        try:
            import pynput  # noqa: F401
        except ImportError:
            return False, "python package 'pynput' is not installed"
        return True, "X11 global hotkey ready"

    def start(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        from pynput import keyboard

        self._on_press = on_press
        self._on_release = on_release

        def key_name(key) -> str | None:
            raw = getattr(key, "name", None) or str(key).replace("Key.", "")
            for modifier, keys in PYNPUT_KEYS.items():
                if raw in keys:
                    return modifier
            return None

        def handle(key, is_press: bool) -> None:
            modifier = key_name(key)
            if modifier is None:
                return
            with self._lock:
                if is_press:
                    self._pressed.add(modifier)
                else:
                    self._pressed.discard(modifier)
                satisfied = all(part in self._pressed for part in self.combo)
                if satisfied and not self._is_down:
                    self._is_down = True
                    self._on_press()
                elif not satisfied and self._is_down:
                    self._is_down = False
                    self._on_release()

        self._listener = keyboard.Listener(
            on_press=lambda key: handle(key, True),
            on_release=lambda key: handle(key, False),
        )
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


class GnomeHotkeyBackend(HotkeyBackend):
    """Single keybinding that toggles listening through the app's IPC socket."""

    id = "gnome"
    display_name = "GNOME custom keybinding"
    toggle_mode = True
    SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/heyclicky/"

    # GTK accelerator strings expect modifiers in this canonical order.
    MODIFIER_ORDER = ("ctrl", "alt", "shift", "super")

    def __init__(self, combo: str = "ctrl+alt") -> None:
        super().__init__(combo)
        self._pressed = False
        self._installed = False

    def available(self) -> tuple[bool, str]:
        if "fn" in self.combo:
            return False, "GNOME keybindings cannot bind the Fn key"
        if not shutil.which("gsettings"):
            return False, "gsettings is not installed"
        if "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            return False, "not a GNOME session"
        return True, "uses a GNOME custom keybinding (toggle mode)"

    def _binding(self) -> str:
        names = {"ctrl": "Control", "alt": "Alt", "shift": "Shift", "super": "Super"}
        ordered = [part for part in self.MODIFIER_ORDER if part in self.combo]
        return "".join(f"<{names[part]}>" for part in ordered) + "space"

    def _command(self) -> str:
        executable = shutil.which("heyclicky")
        if executable:
            return f"{executable} --toggle-listen"
        return f"{sys.executable} -m heyclicky --toggle-listen"

    @staticmethod
    def _parse_entries(current: str) -> list[str]:
        """Parse gsettings' string-array output, including the empty ``@as []`` form."""
        text = (current or "").strip()
        if text.startswith("@as"):
            text = text[len("@as"):].strip()
        if text in ("", "[]"):
            return []
        return [
            entry.strip().strip("'")
            for entry in text.strip("[]").split(",")
            if entry.strip()
        ]

    def install(self) -> None:
        current = subprocess.run(
            ["gsettings", "get", self.SCHEMA, "custom-keybindings"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        entries = self._parse_entries(current)
        if self.PATH not in entries:
            entries.append(self.PATH)
            value = "[" + ", ".join(f"'{entry}'" for entry in entries) + "]"
            subprocess.run(
                ["gsettings", "set", self.SCHEMA, "custom-keybindings", value],
                check=True,
                timeout=10,
            )
        child = f"{self.SCHEMA}.custom-keybinding:{self.PATH}"
        subprocess.run(["gsettings", "set", child, "name", "HeyClicky push-to-talk"], check=True)
        subprocess.run(["gsettings", "set", child, "command", self._command()], check=True)
        subprocess.run(["gsettings", "set", child, "binding", self._binding()], check=True)
        self._installed = True

    def uninstall(self) -> None:
        try:
            current = subprocess.run(
                ["gsettings", "get", self.SCHEMA, "custom-keybindings"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return
        entries = [entry for entry in self._parse_entries(current) if self.PATH not in entry]
        value = "[" + ", ".join(f"'{entry}'" for entry in entries) + "]"
        try:
            subprocess.run(
                ["gsettings", "set", self.SCHEMA, "custom-keybindings", value],
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return
        self._installed = False

    def start(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        # The actual key press arrives through the app's IPC socket as
        # --toggle-listen. Keep the callback for symmetry with other backends.
        self._on_press = on_press
        self._on_release = on_release
        self.install()

    def stop(self) -> None:
        if self._installed:
            self.uninstall()

    def detail(self) -> str:
        text = f"{self.display_name} ({self._binding()}, toggle)"
        if self.note:
            text = f"{text} — {self.note}"
        return text


class ManualHotkeyBackend(HotkeyBackend):
    id = "manual"
    display_name = "In-app button only"
    toggle_mode = True

    def available(self) -> tuple[bool, str]:
        return True, "use the panel or tray menu"

    def start(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        self._on_press = on_press
        self._on_release = on_release

    def stop(self) -> None:
        pass

    def detail(self) -> str:
        return self.display_name


BACKEND_CLASSES: dict[str, type[HotkeyBackend]] = {
    "evdev": EvdevHotkeyBackend,
    "pynput": PynputHotkeyBackend,
    "gnome": GnomeHotkeyBackend,
    "manual": ManualHotkeyBackend,
}


def _safe_parse(combo: str) -> list[str] | None:
    try:
        return parse_combo(combo)
    except HotkeyError:
        return None


def _build_backend(backend_class: type[HotkeyBackend], combo: str, mode: str) -> HotkeyBackend:
    backend = backend_class(combo)
    inherent_toggle = bool(getattr(backend_class, "toggle_mode", False))
    backend.toggle_mode = inherent_toggle or mode == "toggle"
    return backend


def _has_fn(combo: list[str] | None) -> bool:
    return bool(combo and "fn" in combo)


def resolve_hotkey_backend(config, forced: str | None = None) -> HotkeyBackend:
    """Pick the best backend for the configured combo.

    ``hotkey.mode`` is ``auto``, ``hold`` or ``toggle``. ``auto`` means toggle
    for Fn-based chords (they are awkward to hold) and hold otherwise.

    Some laptop firmwares never report the Fn key to the kernel. When the
    requested combo contains ``fn`` and nothing can observe it, the resolver
    retries with ``hotkey.fallback_combo`` and records a note that is shown in
    the panel and in ``--doctor``.
    """
    configured_combo = str(config.get("hotkey.combo", "ctrl+alt") or "ctrl+alt")
    fallback_combo = str(config.get("hotkey.fallback_combo", "ctrl+shift") or "ctrl+shift")
    if _safe_parse(fallback_combo) is None:
        fallback_combo = "ctrl+shift"
    requested_combo = configured_combo if _safe_parse(configured_combo) else fallback_combo

    mode = str(config.get("hotkey.mode", "auto") or "auto").lower()
    if mode not in ("auto", "hold", "toggle"):
        mode = "auto"
    if mode == "auto":
        mode = "toggle" if _has_fn(_safe_parse(requested_combo)) else "hold"

    requested_backend = (forced or config.get("hotkey.backend", "auto")).lower()
    if requested_backend != "auto":
        backend_class = BACKEND_CLASSES.get(requested_backend)
        if backend_class is None:
            raise HotkeyError(f"unknown hotkey backend: {requested_backend}")
        return _build_backend(backend_class, requested_combo, mode)

    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    order = ["evdev", "pynput"] if session != "wayland" else ["evdev", "gnome"]

    candidates = [requested_combo]
    requested_mods = _safe_parse(requested_combo)
    if _has_fn(requested_mods) and set(_safe_parse(fallback_combo) or []) != set(requested_mods or []):
        candidates.append(fallback_combo)

    for index, combo in enumerate(candidates):
        for backend_id in order:
            backend = _build_backend(BACKEND_CLASSES[backend_id], combo, mode)
            ok, _detail = backend.available()
            if not ok:
                continue
            if index > 0:
                backend.requested_combo = requested_combo
                backend.note = (
                    f"{hotkey_label(requested_combo)} is unavailable on this keyboard "
                    f"(firmware does not report Fn), using {hotkey_label(combo)}"
                )
            return backend

    manual = _build_backend(ManualHotkeyBackend, requested_combo, "toggle")
    manual.note = "no global shortcut backend available"
    return manual


def watch_keys(seconds: float = 15.0, stream=None) -> int:
    """Print every key event for a few seconds (diagnostic for Fn keys).

    Run with sufficient permissions (usually ``sudo heyclicky --watch-keys``),
    press the chord you want, and look for ``KEY_FN``.
    """
    import select

    from evdev import InputDevice, ecodes, list_devices

    output = stream or sys.stdout
    devices = []
    for path in list_devices():
        try:
            device = InputDevice(path)
        except (PermissionError, OSError):
            continue
        capabilities = set(device.capabilities().get(1, []))
        if 29 in capabilities and 97 in capabilities:
            devices.append(device)
        else:
            device.close()

    if not devices:
        print(
            "Could not open any keyboard. Re-run with sudo, e.g. "
            "sudo .venv/bin/heyclicky --watch-keys",
            file=output,
        )
        return 1

    print(
        f"Listening to {len(devices)} keyboard(s) for {seconds:.0f}s. "
        "Press Fn+Super (or any chord) now.",
        file=output,
    )
    seen: set[int] = set()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                ready, _, _ = select.select(devices, [], [], 0.2)
            except (OSError, ValueError):
                break
            for device in ready:
                try:
                    events = list(device.read())
                except OSError:
                    continue
                for event in events:
                    if event.type != ecodes.EV_KEY:
                        continue
                    seen.add(event.code)
                    name = ecodes.KEY.get(event.code) or f"KEY_{event.code}"
                    action = {0: "up", 1: "down", 2: "repeat"}.get(event.value, str(event.value))
                    print(f"  {device.name}: {name} {action} (code {event.code})", file=output, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for device in devices:
            try:
                device.close()
            except OSError:
                pass

    if FN_KEY_CODE in seen:
        print("KEY_FN was reported: Fn chords are usable with the evdev backend.", file=output)
    else:
        supported, detail = fn_key_supported()
        if supported:
            print(f"KEY_FN was not pressed, but this keyboard supports it ({detail}).", file=output)
        else:
            print(
                f"No KEY_FN event arrived. {detail} "
                "The kernel cannot see the Fn key, so use a different combo.",
                file=output,
            )
    return 0

