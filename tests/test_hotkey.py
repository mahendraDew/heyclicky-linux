from __future__ import annotations

import subprocess

import pytest

from heyclicky.config import Config
from heyclicky.hotkey import (
    BACKEND_CLASSES,
    GnomeHotkeyBackend,
    HotkeyBackend,
    HotkeyError,
    ManualHotkeyBackend,
    fn_key_supported,
    hotkey_label,
    parse_combo,
    resolve_hotkey_backend,
)


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("ctrl+alt", ["ctrl", "alt"]),
        ("Control+Option", ["ctrl", "alt"]),
        ("shift + super", ["shift", "super"]),
        ("meta", ["super"]),
        ("fn+super", ["fn", "super"]),
        ("Function+Windows", ["fn", "super"]),
        ("", ["ctrl", "alt"]),
    ],
)
def test_parse_combo(combo, expected):
    assert parse_combo(combo) == expected


def test_parse_combo_rejects_unknown_modifier():
    with pytest.raises(HotkeyError):
        parse_combo("ctrl+hyper")


def test_hotkey_label():
    assert hotkey_label("ctrl+alt") == "Ctrl+Alt"
    assert hotkey_label("fn+super") == "Fn+Super"
    assert hotkey_label("nonsense") == "nonsense"


def _write_key_capabilities(root, event, codes):
    path = root / event / "device" / "capabilities"
    path.mkdir(parents=True)
    bits = sum(1 << code for code in codes)
    words = []
    while bits:
        words.append(bits & ((1 << 64) - 1))
        bits >>= 64
    text = " ".join(f"{word:016x}" for word in reversed(words or [0]))
    (path / "key").write_text(text + "\n")


def test_fn_key_supported_reads_sysfs_word_order(tmp_path):
    # 464 is KEY_FN. The bitmap is written most-significant word first, which
    # is easy to get backwards and was a real bug source during this port.
    _write_key_capabilities(tmp_path, "event3", {29, 56, 97, 464})
    supported, detail = fn_key_supported(str(tmp_path))
    assert supported is True
    assert "event3" in detail
    assert "KEY_FN" in detail


def test_fn_key_supported_without_fn(tmp_path):
    _write_key_capabilities(tmp_path, "event3", {29, 56, 97, 125})
    supported, detail = fn_key_supported(str(tmp_path))
    assert supported is False
    assert "firmware" in detail


def test_fn_key_supported_ignores_non_keyboards(tmp_path):
    _write_key_capabilities(tmp_path, "event9", {464})
    supported, _detail = fn_key_supported(str(tmp_path))
    assert supported is False


def test_gnome_backend_rejects_fn():
    ok, detail = GnomeHotkeyBackend("fn+super").available()
    assert ok is False
    assert "Fn" in detail


class _TogglelessFake(HotkeyBackend):
    id = "fake"
    display_name = "Fake"
    fn_ok = False

    def available(self):
        if "fn" in self.combo and not self.fn_ok:
            return False, "no fn"
        return True, "ok"

    def start(self, on_press, on_release):
        pass

    def stop(self):
        pass


def _config_with(monkeypatch, combo, backend="auto", mode="auto"):
    config = Config()
    config.set("hotkey.combo", combo)
    config.set("hotkey.backend", backend)
    config.set("hotkey.mode", mode)
    config.set("hotkey.fallback_combo", "super+alt")
    for name in ("evdev", "pynput", "gnome"):
        monkeypatch.setitem(BACKEND_CLASSES, name, _TogglelessFake)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    return config


def test_fn_combo_falls_back_when_firmware_hides_fn(monkeypatch):
    config = _config_with(monkeypatch, "fn+super")
    backend = resolve_hotkey_backend(config)
    assert backend.combo == ["super", "alt"]
    assert backend.toggle_mode is True  # auto -> toggle for Fn chords
    assert "Fn+Super" in backend.note
    assert "Super+Alt" in backend.note


def test_fn_combo_used_when_keyboard_reports_fn(monkeypatch):
    monkeypatch.setattr(_TogglelessFake, "fn_ok", True)
    config = _config_with(monkeypatch, "fn+super")
    backend = resolve_hotkey_backend(config)
    assert backend.combo == ["fn", "super"]
    assert backend.toggle_mode is True
    assert backend.note == ""


def test_hold_mode_is_honoured(monkeypatch):
    config = _config_with(monkeypatch, "ctrl+alt", mode="hold")
    backend = resolve_hotkey_backend(config)
    assert backend.toggle_mode is False
    assert "hold" in backend.detail()


def test_manual_backend_is_toggle_mode():
    backend = ManualHotkeyBackend("ctrl+alt")
    assert backend.toggle_mode is True
    assert backend.available()[0] is True
    backend.start(lambda: None, lambda: None)
    assert "button" in backend.detail()


def test_forced_manual_resolution():
    config = Config()
    backend = resolve_hotkey_backend(config, "manual")
    assert isinstance(backend, ManualHotkeyBackend)


def test_gnome_binding_uses_canonical_modifier_order():
    backend = GnomeHotkeyBackend("super+shift")
    assert backend._binding() == "<Shift><Super>space"


def test_gnome_install_and_stop_cleanup(monkeypatch):
    backend = GnomeHotkeyBackend("ctrl+alt")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["gsettings", "get", GnomeHotkeyBackend.SCHEMA]:
            return subprocess.CompletedProcess(command, 0, stdout="@as []\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("heyclicky.hotkey.shutil.which", lambda name: "/usr/bin/heyclicky")

    backend.install()
    assert backend._installed is True
    binding_call = next(cmd for cmd in commands if cmd[-2] == "binding")
    assert binding_call[-1] == "<Control><Alt>space"
    command_call = next(cmd for cmd in commands if cmd[-2] == "command")
    assert command_call[-1] == "/usr/bin/heyclicky --toggle-listen"

    commands.clear()
    backend.stop()
    assert backend._installed is False
    assert commands == [
        ["gsettings", "get", GnomeHotkeyBackend.SCHEMA, "custom-keybindings"],
        ["gsettings", "set", GnomeHotkeyBackend.SCHEMA, "custom-keybindings", "[]"],
    ]


def test_gnome_uninstall_preserves_other_bindings(monkeypatch):
    backend = GnomeHotkeyBackend("ctrl+alt")
    existing = "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/', " \
        f"'{GnomeHotkeyBackend.PATH}']"
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["gsettings", "get", GnomeHotkeyBackend.SCHEMA]:
            return subprocess.CompletedProcess(command, 0, stdout=existing + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend.uninstall()

    written = next(cmd for cmd in commands if cmd[:3] == ["gsettings", "set", GnomeHotkeyBackend.SCHEMA])
    assert written[-1] == (
        "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']"
    )
