"""Configuration for HeyClicky Linux.

The original macOS app persisted a handful of preferences in UserDefaults and
kept every API key on a Cloudflare Worker. A desktop app that talks to Groq,
Gemini, OpenRouter and a local Ollama needs user-supplied keys, so this module
stores them in a 0600 JSON file under the XDG config directory (or environment
variables, optionally loaded from a ``.env`` file in the project root).

Nothing here is read at import time; call :func:`load_config` explicitly so the
path can be overridden in tests.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "heyclicky"
CONFIG_DIR_ENV = "HEYCLICKY_CONFIG_DIR"

# Mirrors the defaults documented in docs/REVERSE_ENGINEERING.md. OpenRouter
# (NVIDIA Nemotron 3.5 Super VL free) is the default provider: it has a large
# context, accepts images (screen pointing works) and is less rate-limited than
# the Gemma free tier.
DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "provider": "openrouter",
        # off | low | model. Reasoning models can spend several seconds (and
        # the whole token budget) thinking before they answer; "off" keeps
        # replies fast and avoids empty responses on small max_tokens.
        "reasoning": "off",
        "openrouter": {
            "api_key": "",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "nvidia/nemotron-3.5-super-vl:free",
        },
        "groq": {
            "api_key": "",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "openai/gpt-oss-20b",
        },
        "gemini": {
            "api_key": "",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-2.5-flash",
        },
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5vl:7b",
            "keep_alive": "10m",
        },
    },
    "stt": {
        "provider": "auto",
        "language": "en",
        "keyterms": [
            "HeyClicky",
            "Clicky",
            "Ollama",
            "Groq",
            "Gemini",
            "Qwen",
            "Ubuntu",
            "Wayland",
            "X11",
            "localhost",
        ],
        "groq_model": "whisper-large-v3-turbo",
        "gemini_model": "gemini-2.5-flash",
        "local_model": "base",
    },
    "tts": {
        "provider": "auto",
        "voice": "en-US-AriaNeural",
        "gemini_model": "gemini-2.5-flash-preview-tts",
        "gemini_voice": "Kore",
        "piper_model": "",
        "volume": 0.9,
        "enabled": True,
    },
    "capture": {
        "backend": "auto",
        "max_dimension": 1280,
        "jpeg_quality": 80,
        "hide_overlay": True,
        # Filled in automatically by the ScreenCast portal so the share
        # permission survives restarts.
        "screencast_restore_token": "",
    },
    "hotkey": {
        # auto | evdev | pynput | gnome | manual
        "backend": "auto",
        # Modifier-only chord. Ctrl+Alt+Space is free on stock GNOME/KDE and
        # does not collide with the shell's Super shortcuts.
        "combo": "ctrl+alt",
        # auto | hold | toggle. auto = toggle for Fn chords, hold otherwise.
        "mode": "auto",
        # Used when the keyboard/firmware cannot report one of the modifiers.
        "fallback_combo": "ctrl+shift",
    },
    "audio": {
        "input_device": "default",
        "sample_rate": 16000,
        "max_seconds": 120,
    },
    "ui": {
        "edge_glow": True,
        "onboarding_completed": False,
        "transient_hide_seconds": 1.0,
        # Show the panel on launch. Tray-only startup is opt-in because some
        # desktops report a tray host but never draw the icon.
        "start_minimized": False,
        "panel_width": 900,
    },
    "privacy": {
        "store_conversation": False,
        "log_level": "info",
    },
}


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_env_file() -> None:
    """Load ``KEY=value`` pairs from a ``.env`` file into os.environ.

    Existing environment variables are never overwritten. The file is looked up
    in, in order: ``$HEYCLICKY_CONFIG_DIR/.env``, ``$XDG_CONFIG_HOME/heyclicky/.env``
    and ``./.env`` relative to the current working directory. A simple parser is
    used so no extra dependency (python-dotenv) is required.
    """
    candidates: list[Path] = []
    config_dir = os.environ.get(CONFIG_DIR_ENV)
    if config_dir:
        candidates.append(Path(config_dir).expanduser() / ".env")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg).expanduser() / APP_NAME / ".env")
    candidates.append(Path.cwd() / ".env")

    seen: set[Path] = set()
    for path in candidates:
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env_overrides() -> dict[str, Any]:
    """Environment variables win so keys never have to be written to disk."""
    overrides: dict[str, Any] = {"llm": {}, "tts": {}}
    if value := os.environ.get("GROQ_API_KEY"):
        overrides["llm"].setdefault("groq", {})["api_key"] = value
    if value := os.environ.get("OPENROUTER_GEMMA_API_KEY") or os.environ.get("OPENROUTER_API_KEY"):
        overrides["llm"].setdefault("openrouter", {})["api_key"] = value
    if value := os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        overrides["llm"].setdefault("gemini", {})["api_key"] = value
    if value := os.environ.get("OLLAMA_HOST"):
        # OLLAMA_HOST is commonly "127.0.0.1:11434" without a scheme.
        if "://" not in value:
            value = f"http://{value}"
        overrides["llm"].setdefault("ollama", {})["base_url"] = value
    if value := os.environ.get("HEYCLICKY_LLM_PROVIDER"):
        overrides["llm"]["provider"] = value
    if value := os.environ.get("HEYCLICKY_TTS_PROVIDER"):
        overrides["tts"]["provider"] = value
    return overrides


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG))
    path: Path | None = None

    # -- generic access -------------------------------------------------
    def get(self, dotted_path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted_path: str, value: Any) -> None:
        parts = dotted_path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise TypeError(f"{'.'.join(parts[:-1])} is not a section")
        node[parts[-1]] = value

    # -- persistence ----------------------------------------------------
    def save(self) -> None:
        target = self.path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)

    def save_public_copy(self, target: Path) -> None:
        """Write the config with API keys redacted (used by --doctor)."""
        redacted = copy.deepcopy(self.data)
        for provider in ("groq", "gemini", "openrouter"):
            section = redacted.get("llm", {}).get(provider, {})
            if section.get("api_key"):
                section["api_key"] = "***redacted***"
        target.write_text(json.dumps(redacted, indent=2) + "\n", encoding="utf-8")


def load_config() -> Config:
    _load_env_file()
    stored: dict[str, Any] = {}
    path = config_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A malformed config must not prevent the app from starting; keep a
            # backup so the user can recover hand-edited content.
            try:
                path.replace(path.with_suffix(".json.broken"))
            except OSError:
                pass
            stored = {}
    data = _deep_merge(DEFAULT_CONFIG, stored)
    data = _deep_merge(data, _env_overrides())
    # fn+super was the old default, but most laptop firmware never exposes the
    # Fn key to Linux, so those installs silently fell back to a Super chord
    # that GNOME often reserves for its own shortcuts. Move old installs to the
    # free Ctrl+Alt+Space binding; users can still pick fn+super in Settings.
    hotkey = data.setdefault("hotkey", {})
    if str(hotkey.get("combo", "")).strip().lower() == "fn+super":
        hotkey["combo"] = "ctrl+alt"
        if str(hotkey.get("fallback_combo", "")).strip().lower() in ("", "super+alt"):
            hotkey["fallback_combo"] = "ctrl+shift"
    return Config(data=data, path=path)


def provider_key(config: Config, provider: str) -> str:
    return str(config.get(f"llm.{provider}.api_key", "") or "")
